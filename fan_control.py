#!/usr/bin/env python3

# ==================================================================================================
#   IMPORT
#

import argparse
import os
import sensors     # https://github.com/bastienleonard/pysensors.git
import signal
import subprocess
import sys
import threading
import time
import yaml


# ==================================================================================================
#   CLASS
#

class Logger:

    debug = False

    def __init__( self, debug: bool ):
        self.debug = debug

    def _print( self, level: str, msg: str ):
        if level == "debug" and not self.debug:
            return

        print( "{}: {}".format(
            level.upper(),
            msg
        ))

    def pdebug( self, msg: str ):
        self._print( "debug", msg )

    def pinfo( self, msg: str ):
        self._print( "info", msg )

    def pwarn( self, msg: str ):
        self._print( "warn", msg )

    def perror( self, msg: str ):
        self._print( "error", msg )

class Config():

    class ConfigKeyError( Exception ): pass

    class ConfigPathError( Exception ): pass

    class General(): pass

    class Host(): pass

    general = General
    hosts = []

    def __init__( self, path, interval, verbose ):
        if not os.path.isfile( path ):
            raise Config.ConfigPathError( "{}: no such file or directory".format( path ) )

        config = None
        try:
            with open( path, "r" ) as content:
                config = yaml.safe_load( content )
        except yaml.YAMLError as err:
            raise err

        self.general = config[ "general" ]
        if interval != None:
            self.general[ "interval" ] = interval
        if verbose != None and verbose == True:
            self.general[ "debug" ] = verbose

        log = Logger( self.general[ "debug" ] )

        self.hosts = config[ "hosts" ]
        for host in self.hosts:
            # Hysterisis
            if "hysterisis" not in list( host.keys() ):
                host[ "hysterisis" ] = 0
                log.pwarn( "hysterisis not defined... setting it to 0°C" )

            # Temperature and speed
            if "threshold" not in list( host.keys() ):
                log.perror( "no threshold defined for {}".format( host[ "name" ] ) )
                raise Config.ConfigKeyError()
            for t in host[ "threshold" ]:
                # Temperature
                # Need to find what to check

                # Speed
                if t[ "speed" ] < 5:
                    log.pwarn( "minimum speed is 5%" )
                    t[ "speed" ] = 5
                if t[ "speed" ] > 100:
                    log.pwarn( "maximum speed is 100%" )
                    t[ "speed" ] = 100

class FanControl:

    cfg_gen  = Config.General
    cfg_host = Config.Host

    cmd = [ "ipmitool" ]
    state = { "temperature": -1, "speed": 100, "mode": "automatic" }
    is_remote_host = False

    run = False

    def __init__( self, cfg_gen: Config.General, cfg_host: Config.Host, log: Logger ):
        self.cfg_gen = cfg_gen
        self.cfg_host = cfg_host

        if cfg_host[ "type" ] == "remote":
            self.is_remote_host = True

            self.cmd += [ "-I", "lanplus" ]
            self.cmd += [ "-H", cfg_host[ "remote_cfg" ][ "host" ] ]
            self.cmd += [ "-U", cfg_host[ "remote_cfg" ][ "creds" ][ "user" ] ]
            self.cmd += [ "-P", cfg_host[ "remote_cfg" ][ "creds" ][ "pass" ] ]

    def execute( self ):
        for t in self.cfg_host[ "threshold" ]:
            log.pinfo( "[{}] threshold of {}°C => {}%".format(
                self.cfg_host[ "name" ], t[ "temperature" ], t[ "speed" ]
            ))

        self.run = True
        while self.run:
            temps = []

            if not self.is_remote_host:
                cores = []
                for sensor in sensors.get_detected_chips():
                    if sensor.prefix == "coretemp":
                        cores.append( sensor )
                for core in cores:
                    for feature in core.get_features():
                        for subfeature in core.get_all_subfeatures( feature ):
                            if subfeature.name.endswith( "_input" ):
                                temps.append( core.get_value( subfeature.number ) )

            else:
                cmd = os.popen( self.cfg_host[ "remote_cfg" ][ "command" ] )
                temps = list( map( lambda n: float( n ), cmd.read().strip().split( '\n' ) ) )
                cmd.close()

            temp_average = round( sum( temps ) / len( temps ) )
            log.pinfo( "[{}] average temperature {}°C".format( self.cfg_host[ "name" ], temp_average ) )
            for idx, temp in enumerate( temps ):
                log.pdebug( "[{}] core {} => {}°C".format( self.cfg_host[ "name" ], idx, temp ) )

            need_to_fallback = True
            prev_threshold = 0
            curr_threshold = None
            for t in self.cfg_host[ "threshold" ]:
                curr_threshold = t[ "temperature" ]

                # Check hysterisis
                hysteresis_ok = True
                if self.cfg_host[ "hysterisis" ]:
                    if ( self.state[ "speed" ] > t[ "speed" ] or self.state[ "mode" ] == "automatic" ):
                        hysteresis_ok = ( temp_average <= ( curr_threshold - self.cfg_host[ "hysterisis" ] ) )

                # Compute fan speed
                if ( temp_average > prev_threshold and temp_average <= curr_threshold and hysteresis_ok ):
                    self.set_fan_speed( t[ "speed" ] )
                    need_to_fallback = False
                    break

                # Assign previous threshold for next loop
                prev_threshold = curr_threshold

            if need_to_fallback:
                log.pwarn( "[{}] fallback needed for {}°C".format( self.cfg_host[ "name" ], temp_average ) )
                self.set_fan_speed( 100 )

            time.sleep( self.cfg_gen[ "interval" ] )

    def stop( self ):
        log.pinfo( "stopping execution" )
        self.run = False
        self.set_fan_control( "automatic" )

    def send_cmd( self, args: list ):
        cmd = self.cmd + ( args.split( ' ' ) )
        log.pdebug( "[{}] command: {}".format( self.cfg_host[ "name" ], cmd ) )

        try:
            subprocess.check_output( cmd, timeout=15 )
        except subprocess.CalledProcessError:
            return False
        except subprocess.TimeoutExpired:
            return False

        return True

    def set_fan_control( self, mode: str ):
        if mode != "automatic" and mode != "manual":
            mode = "automatic"

        if mode == "manual" and self.state[ "mode" ] != "manual":
            self.send_cmd( "raw 0x30 0x30 0x01 0x00" )

        if mode == "automatic" and self.state[ "mode" ] != "automatic":
            self.send_cmd( "raw 0x30 0x30 0x01 0x01" )
            self.state[ "speed" ] = 0

        log.pinfo( "[{}] setting fan mode to {}".format( self.cfg_host[ "name" ], mode ) )
        self.state[ "mode" ] = mode

    def set_fan_speed( self, speed: int ):
        speed_hex = "{0:#0{1}x}".format( speed, 4 )

        if self.state[ "mode" ] != "manual":
            self.set_fan_control( "manual" )
            time.sleep( 1 )

        if self.state[ "speed" ] == speed:
            return

        log.pinfo( "[{}] setting fan speed to {}%".format( self.cfg_host[ "name" ], speed ) )
        self.send_cmd( "raw 0x30 0x30 0x02 0xff {}".format( speed_hex ) )
        self.state[ "speed" ] = speed


# ==================================================================================================
#   MAIN
#

threads = []

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c", "--config",
        help="configuration file path",
        type=str,
        default="/etc/fan_controller/config.yaml"
    )
    parser.add_argument(
        "-i", "--interval",
        help="interval to run check",
        type=int
    )
    parser.add_argument(
        "-v", "--verbose",
        help="increase output verbosity",
        action="store_true"
    )
    args = parser.parse_args()

    try:
        config = Config( args.config, args.interval, args.verbose )
    except ( Config.ConfigKeyError, Config.ConfigPathError ):
        sys.exit( 1 )

    log = Logger( config.general[ "debug" ] )

    # Reset fan control to automatic when getting killed
    def shutdown( signalnum, frame ):
        log.pinfo( "signal {} received".format( signalnum ) )
        for thread in threads:
            thread[ "host" ].stop()
            thread[ "thread" ].join()
        sys.exit( 0 )
    signal.signal( signal.SIGTERM, shutdown )
    signal.signal( signal.SIGINT, shutdown )

    try:
        for cfg_host in config.hosts:
            host = FanControl( config.general, cfg_host, log )
            x = threading.Thread( target=host.execute, args=() )
            threads.append({ "host": host, "thread": x })
            x.start()
        while True:
            time.sleep( 1 )
    finally:
        sensors.cleanup()
