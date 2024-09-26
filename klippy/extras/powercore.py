from pwm_in import PWMIn
import math
from typing import TYPE_CHECKING
from simple_pid import PID
import logging
from toolhead import Move
from load_cell import load_config as load_cell_load_config
from load_cell import LoadCell
from klippy.virtual_configfile import VirtualConfigFile
from pwm_tool import PrinterOutputPin

if TYPE_CHECKING:
    from ..toolhead import ToolHead
    from ..configfile import ConfigWrapper
    from ..klippy import Printer
    from ..gcode import GCodeDispatch
    from ..reactor import SelectReactor as Reactor


class PowerCore:
    def __init__(self, config: "ConfigWrapper"):
        self._pwm_reader = PowerCorePWMReader(config)
        self._pwm_reader.setup_callback(self.pwm_in_callback)
        self.printer: "Printer" = config.get_printer()
        self.reactor: "Reactor" = self.printer.get_reactor()
        self.toolhead: "ToolHead" = None
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.gcode: "GCodeDispatch" = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "GET_POWERCORE_DUTY_CYCLE",
            self.cmd_get_duty_cycle,
            desc="Get the current duty cycle",
        )
        self.gcode.register_command(
            "ENABLE_POWERCORE_FEED_SCALING",
            self.cmd_enable_scaling,
            desc="Enable powercore feedrate scaling",
        )
        self.gcode.register_command(
            "DISABLE_POWERCORE_FEED_SCALING",
            self.cmd_disable_scaling,
            desc="Disable powercore feedrate scaling",
        )
        self.gcode.register_command(
            "SET_POWERCORE_PID",
            self.cmd_set_pid_params,
            desc="Set powercore pid params",
        )
        self.gcode.register_command(
            "RESET_POWERCORE_PID",
            self.cmd_reset_pid,
            desc="resets pid controller, recommended to do at the start of a cut",
        )
        self.gcode.register_command(
            "SET_POWERCORE_TARGET_DUTY_CYCLE",
            self.cmd_set_target_duty_cycle,
            desc="set the target powercore target duty cycle",
        )
        self.gcode.register_command(
            "SET_POWERCORE_FEED_RANGE",
            self.cmd_set_powercore_feedrates,
            desc="set powercore feedrates",
        )
        self.target_duty_cycle: float = config.getfloat(
            "target_duty_cycle", 0.75, minval=0.0, maxval=1.0
        )
        self.min_feedrate: float = config.getfloat(
            "min_feedrate", 6.0, minval=1.0
        )  # mm/min
        self.max_feedrate: float = config.getfloat(
            "max_feedrate", 120.0, minval=self.min_feedrate
        )  # mm/min
        self.adjustment_accel = config.getfloat(
            "powercore_adjustment_accel", 5000.0, above=0.0
        )

        # debug verbosity, helpful for tuning the pid controller
        self.verbose_pid_output = config.getboolean("verbose_pid_output", False)
        self.verbose_move_scaling_output = config.getboolean(
            "verbose_move_scaling_output", False
        )

        self.feedrate_pid_controller = PID(
            Kp=config.getfloat("pid_kp", 0.1),
            Ki=config.getfloat("pid_ki", 0.0),
            Kd=config.getfloat("pid_kd", 0.0),
            setpoint=self.target_duty_cycle,
            output_limits=(0, 1),
            sample_time=self._pwm_reader.sample_interval,
            time_fn=self.reactor.monotonic,
        )
        self.move_split_dist = config.getfloat(
            "move_split_dist", 0.1, above=0.0
        )  # segment size for move splitting
        self.move_overlap_time = config.getfloat("move_overlap_time", 0.001)
        # 0 would mean we finish one move segment completely before starting the next
        # this is likely coupled tightly with move_split_dist and the feedrate being used

        self.scaling_enabled = False
        self.move_with_transform = None

        ### Wire tension control
        self.wire_load_cell: LoadCell = load_cell_load_config(config)
        self.wire_load_cell.sensor.add_client(self.wire_tension_callback)

        self.default_wire_tension_target = config.getfloat(
            "default_wire_tension_target", 0.0
        )
        self.wire_pid_controller = PID(
            Kp=config.getfloat("wire_pid_kp", 0.1),
            Ki=config.getfloat("wire_pid_ki", 0.0),
            Kd=config.getfloat("wire_pid_kd", 0.0),
            setpoint=self.default_wire_tension_target,
            output_limits=(0, 1),
            sample_time=self.wire_load_cell.sensor.UPDATE_INTERVAL,
            # TODO: could make this configurable?
            time_fn=self.reactor.monotonic,
        )
        self.wire_tension_loop_enabled = True
        self.sender_wire_tool = WireMover(
            self.toolhead,
            config.get("sender_wire_motor_pin"),
            config.getfloat("sender_wire_cycle_time", 0.1),
            config.getboolean("sender_wire_hardware_pwm", False),
            config.getfloat("sender_wire_scale", 1.0),
        )
        self.receiver_wire_tool = WireMover(
            self.toolhead,
            config.get("receiver_wire_motor_pin"),
            config.getfloat("receiver_wire_cycle_time", 0.1),
            config.getboolean("receiver_wire_hardware_pwm", False),
            config.getfloat("receiver_wire_scale", 1.0),
        )
        self.sender_is_primary = config.getboolean("sender_is_primary", True)
        self.gcode.register_command(
            "ENABLE_WIRE_TENSION_LOOP",
            self.cmd_ENABLE_WIRE_TENSION_LOOP,
            desc="enable/disable wire tension loop with ENABLE param",
        )
        self.gcode.register_command(
            "SET_WIRE_TENSION_TARGET",
            self.cmd_SET_WIRE_TENSION_TARGET,
            desc="set the wire tension target",
        )
        self.gcode.register_command(
            "SET_WIRE_FEED",
            self.cmd_SET_WIRE_FEED,
            desc="set the wire feed speed",
        )
        self.gcode.register_command(
            "RESET_WIRE_TENSION_PID",
            self.cmd_RESET_WIRE_TENSION_PID,
            desc="reset the wire tension pid controller",
        )
        # TODO - load routines / macros
        # TODO - invidual motor control commands for debugging
        # TODO - general logging for debugging

    def wire_tension_callback(self, msg: dict):
        # this is called every time the load cell sends a new sample
        # this timing is controlled by the load cell's update interval
        # default is 0.1s (set in `UPDATE_INTERVAL` in the sensor classes)
        if not self.wire_tension_loop_enabled:
            return
        samples = msg["data"]
        sample = samples[-1]
        # TODO: what units is the load sensor in?
        # do we need to normalize the data result at all?
        # probably should convert into grams or something?
        # - probably not hard to do or already done in the hx code

        # uses the bulk sensor api.
        # samples has all the samples since the last callback
        # for now, only get the last sample
        # there might be a better way to do the pid to account for all of the samples,
        # but we'd need timing info for each one, so the pid controller knows what's up.
        # otherwise, it could end up super jittery.
        output = self.wire_pid_controller(sample)
        # scale output from 0-1 to 0-255 for motor speed control
        output = round(output * 255)
        if self.sender_is_primary:
            self.receiver_wire_tool.set_speed(output)
        else:
            self.sender_wire_tool.set_speed(output)

    def cmd_SET_WIRE_FEED(self, gcmd):
        speed = gcmd.get_float("speed")
        # if the tension loop is not running, set both motors to the same speed
        # otherwise, only set the primary motor speed - the control loop will handle the secondary motor
        if not self.wire_tension_loop_enabled:
            self.sender_wire_tool.set_speed(speed)
            self.receiver_wire_tool.set_speed(speed)
        else:
            if self.sender_is_primary:
                self.sender_wire_tool.set_speed(speed)
            else:
                self.receiver_wire_tool.set_speed(speed)
        gcmd.respond_info(f"Wire feed speed set to: {speed}")

    def cmd_SET_WIRE_TENSION_TARGET(self, gcmd):
        # TODO: what units is the load sensor in?
        target = gcmd.get_float("TARGET")
        self.wire_pid_controller.setpoint = target
        gcmd.respond_info(f"Wire tension target set to: {target}")

    def cmd_ENABLE_WIRE_TENSION_LOOP(self, gcmd):
        enable = gcmd.get_int("ENABLE")
        should_enable = bool(enable)
        self.wire_tension_loop_enabled = should_enable
        if self.wire_tension_loop_enabled:
            # reset the pid controller when enabling the loop, we are starting fresh
            self.wire_pid_controller.reset()
            gcmd.respond_info("Wire tension loop enabled")
        else:
            gcmd.respond_info("Wire tension loop disabled")

    def cmd_RESET_WIRE_TENSION_PID(self, gcmd):
        self.wire_pid_controller.reset()
        gcmd.respond_info("Wire tension PID controller reset")

    def pwm_in_callback(self, duty_cycle):
        if not self.scaling_enabled:
            return
        self.output = self.feedrate_pid_controller(duty_cycle)
        if self.verbose_pid_output:
            logging.info(f"PowerCore PID-loop output: {self.output}")

    def patch_kinematics_module(self):
        kin = self.toolhead.kin
        kin.old_check_move = kin.check_move
        self.kin_check_move = kin.old_check_move
        kin.check_move = self.check_move

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        gcode_move = self.printer.lookup_object("gcode_move")
        self.move_with_transform = gcode_move.set_move_transform(
            self, force=True
        )
        self.patch_kinematics_module()

    def cmd_set_powercore_feedrates(self, gcmd):
        min_feedrate = gcmd.get_float("MIN", self.min_feedrate)
        max_feedrate = gcmd.get_float("MAX", self.max_feedrate)
        if min_feedrate > max_feedrate:
            gcmd.respond_error(
                f"Min feedrate must be greater than max feedrate! ({min_feedrate} > {max_feedrate})"
            )
        else:
            self.min_feedrate = min_feedrate
            self.max_feedrate = max_feedrate
            gcmd.respond_info(
                f"Min feedrate: {min_feedrate}, max_feedrate: {max_feedrate}"
            )

    def cmd_reset_pid(self, gcmd):
        self.feedrate_pid_controller.reset()
        gcmd.respond_info("Reset powercore PID")

    def cmd_set_target_duty_cycle(self, gcmd):
        target_duty_cycle = gcmd.get_float("DUTY_CYCLE", self.target_duty_cycle)
        self.target_duty_cycle = target_duty_cycle
        self.feedrate_pid_controller.setpoint = target_duty_cycle
        gcmd.respond_info(f"Target duty cycle: {target_duty_cycle}")

    def cmd_set_pid_params(self, gcmd):
        kp = gcmd.get_float("KP", self.feedrate_pid_controller.Kp)
        ki = gcmd.get_float("KI", self.feedrate_pid_controller.Ki)
        kd = gcmd.get_float("KD", self.feedrate_pid_controller.Kd)
        self.feedrate_pid_controller.tunings = (kp, ki, kd)
        gcmd.respond_info(f"PID Params: Kp: {kp}, Ki: {ki}, Kd: {kd}")

    def cmd_get_duty_cycle(self, gcmd):
        duty_cycle = self._pwm_reader.get_current_duty_cycle()
        gcmd.respond_info(f"duty_cycle: {duty_cycle}")

    def cmd_enable_scaling(self, gcmd):
        self.enable_scaling()
        gcmd.respond_info("Enabled powercore move scaling")

    def cmd_disable_scaling(self, gcmd):
        self.disable_scaling()
        gcmd.respond_info("Disabled powercore move scaling")

    def enable_scaling(self):
        self.feedrate_pid_controller.reset()
        self.scaling_enabled = True

    def disable_scaling(self):
        self.scaling_enabled = False

    def check_move(self, move: "Move"):
        self.kin_check_move(move)
        if not self.scaling_enabled:
            return
        else:
            self.scale_move(move)

    def scale_move(self, move: "Move"):
        output = self.output
        # output it 0-1, scale it to min_feedrate-max_feedrate
        feedrate = self.min_feedrate + output * (
            self.max_feedrate - self.min_feedrate
        )
        # feedrate is in mm/min, set_speed expects mm/sec
        feedrate = feedrate / 60
        move.set_speed(feedrate, self.adjustment_accel)
        if self.verbose_move_scaling_output:
            logging.info(f"move time: {move.min_move_t}")
            logging.info(f"output: {output}, feedrate: {feedrate}mm/s")

    def execute_moves(self, moves):
        while len(moves):
            self.processing_moves = True
            move = moves.pop(0)
            try:
                self.toolhead.move(move[1], move[2])
                wake_time = None

                def move_timing_callback(next_move_time):
                    nonlocal wake_time
                    wake_time = next_move_time - self.move_overlap_time

                self.toolhead.register_lookahead_callback(move_timing_callback)
                self.toolhead.lookahead.flush()  # process move immediately
                if wake_time:
                    self.reactor.pause(wake_time)
            except self.gcode.error as e:
                self.gcode._respond_error(str(e))
                self.printer.send_event("gcode:command_error")
                return

    def move(self, newpos, speed):
        if self.scaling_enabled:
            split_positions = self.split_move(newpos, speed)
            self.execute_moves(split_positions)
        else:
            self.toolhead.move(newpos, speed)

    def get_position(self):
        x, y, z, e = self.toolhead.get_position()
        return [x, y, z, e]

    def split_move(
        self, newpos, speed
    ) -> list[tuple[tuple[float], tuple[float]]]:
        # split move into segments based on time
        # time is in seconds
        pos = self.get_position()
        move = Move(self.toolhead, pos, newpos, speed, skip_junction=True)
        target_move_length = self.move_split_dist  # in mm
        move_dist = move.move_d
        total_num_segments = math.ceil(move_dist / target_move_length)
        actual_move_length = move_dist / total_num_segments
        move_vector = move.axes_r  # normalized vector, list
        first_move_end_pos = [
            round(move.start_pos[i] + (actual_move_length * v), 3)
            for i, v in enumerate(move_vector)
        ]
        first_pos = [move.start_pos, first_move_end_pos, speed]

        split_positions = [first_pos]
        for _ in range(total_num_segments - 1):
            start_pos = split_positions[-1][1]
            end_pos = [
                round(start_pos[i] + (actual_move_length * v), 3)
                for i, v in enumerate(move_vector)
            ]
            split_positions.append([start_pos, end_pos, speed])
        last_move = split_positions[-1]
        last_move[1] = move.end_pos
        result = [tuple(pos) for pos in split_positions]
        # logging.info(f"move {newpos} split into: {[tup[0] for tup in result]}")
        return result


class PowerCorePWMReader:
    def __init__(self, config):
        printer = config.get_printer()
        self._pwm_in = None

        pin = config.get("alrt_pin")
        pwm_frequency = config.getfloat(
            "alrt_minimum_pwm_frequency", 10000.0, above=1000.0
        )
        self.sample_interval = config.getfloat(
            "alrt_sample_interval", 0.1, above=0.1
        )
        self._pwm_in = PWMIn(printer, pin, self.sample_interval, pwm_frequency)

    def setup_callback(self, cb):
        self._pwm_in.setup_callback(cb)

    def get_current_duty_cycle(self):
        return round(self._pwm_in.get_duty_cycle(), 3)


class WireMover:
    def __init__(self, toolhead, pin, cycle_time, hardware_pwm, scale):
        values = {
            "pin": pin,
            "cycle_time": cycle_time,
            "hardware_pwm": hardware_pwm,
            "scale": scale,
        }
        config = VirtualConfigFile(values)
        self.pwm_tool = PrinterOutputPin(config)
        self.toolhead = toolhead

    def set_speed(self, speed):
        print_time = self.toolhead.get_last_move_time()
        self.pwm_tool._set_pin(print_time, speed)


def load_config(config):
    return PowerCore(config)