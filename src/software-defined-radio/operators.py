######################################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
######################################################################################################

import time
import socket
import copy

from enum import Enum

import cupy as cp
import cupyx.scipy.signal as cusignal

from holoscan.core import Operator, OperatorSpec
from common import setup_logging


def extract_channel_signal(signal_in, channel_index, logger=None):
    """Extract a specific channel from multi-channel input signal

    Args:
        signal_in: Input signal (1D or 2D CuPy array)
        channel_index: Index of channel to extract
        logger: Optional logger for error messages

    Returns:
        Extracted 1D channel signal, or None if error
    """
    # Extract the specific channel from multi-channel input
    if signal_in.ndim == 2:
        if channel_index >= signal_in.shape[1]:
            error_msg = f"Channel index {channel_index} out of range for {signal_in.shape[1]} channels"
            if logger:
                logger.error(error_msg)
            else:
                print(f"ERROR: {error_msg}")
            return None
        return signal_in[:, channel_index]
    else:
        # Handle case where input is already 1D (backwards compatibility)
        return signal_in


class L4Proto(Enum):
    TCP = 0
    UDP = 1

class NetworkOpBurstParams:
    header: bytearray
    data: bytearray
    def reset(self):
        self.header = bytearray()
        self.data = bytearray()
    def __init__(self):
        self.reset()

def fm_demod(x: cp.array, axis=-1):
    """ Demodulate Frequency Modulated Signal
    """
    x = cp.asarray(x)
    if cp.isrealobj(x):
        raise AssertionError("Input signal must be complex-valued")
    x_angle = cp.unwrap(cp.angle(x), axis=axis)
    y = cp.diff(x_angle, axis=axis)
    return y

def lowpass(taps: cp.array, x: cp.array):
    return cusignal.lfilter(taps, cp.array([1]), x).astype(cp.complex64)

def reduce_fraction(numerator: int, denominator: int, max_up=1):
    max_freq = numerator * float(max_up)
    if max_freq > 10_000_000: # 10 MHz
        raise ValueError(f"max_freq {max_freq} is too high")

    out_freq_sf = round(max_freq / denominator)
    return max_up, out_freq_sf

def float_to_pcm(f_data: cp.array, dtype=cp.int16):
    """
    Function made using the following sources:
    - https://stackoverflow.com/a/15094612
    - https://stackoverflow.com/a/61835960
    - http://blog.bjornroche.com/2009/12/int-float-int-its-jungle-out-there.html
    """
    dtype_max = cp.iinfo(dtype).max
    dtype_min = cp.iinfo(dtype).min
    abs_int_max = 2**(cp.iinfo(dtype).bits -1)
    return cp.clip(f_data*abs_int_max, dtype_min, dtype_max).astype(cp.int16)


class BasicNetworkRxOp(Operator):
    sock_fd: socket.socket = None
    l4_proto: L4Proto = None
    ip_addr: str = None
    dst_port: int = None
    batch_size: int = None
    max_payload_size: int = None
    connected: bool = False
    send_burst: NetworkOpBurstParams = NetworkOpBurstParams()

    def initialize(self):
        Operator.initialize(self)
        try:
            if self.l4_proto == "udp":
                self.l4_proto = L4Proto.UDP
                self.sock_fd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                buffersize = 49_000_000
                self.sock_fd.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffersize)
            else:
                self.l4_proto = L4Proto.TCP
                self.sock_fd = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            self.sock_fd.bind((self.ip_addr, self.dst_port))
            if self.l4_proto == L4Proto.TCP:
                self.sock_fd.listen(1)

            self.logger.info(f"Successfully listening on {self.ip_addr}:{self.dst_port}")
        except socket.error as e:
            self.logger.error(f"Failed to create socket: {e}")

        self.logger.info("Basic RX operator initialized")

    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)

    def setup(self, spec: OperatorSpec):
        # Settings
        spec.param("ip_addr")
        spec.param("dst_port")
        spec.param("l4_proto")
        spec.param("batch_size")
        spec.param("header_bytes")
        spec.param("max_payload_size")
        spec.output("burst_out")

    def compute(self, op_input, op_output, context):
        """
        Input is a TCP/UDP stream not directly managed by holoscan
        burst_out : NetworkOpBurstParams
        burst_out : bytearray
        """
        if self.l4_proto == L4Proto.TCP and not self.connected:
            try:
                self.sock_fd.settimeout(1)
                self.conn, self.addr = self.sock_fd.accept()
                self.logger.info(f"Connected by {self.addr}")
                self.connected = True
            except socket.error:
                return
            finally:
                self.sock_fd.settimeout(None)

        while True:
            try:
                if self.l4_proto == L4Proto.UDP:
                    tmp = self.sock_fd.recvfrom(self.max_payload_size, socket.MSG_DONTWAIT)
                    n = tmp[0]
                else:
                    n = self.conn.recv(self.max_payload_size, socket.MSG_DONTWAIT)
                # Seperate header and payload
                header, data = n[:self.header_bytes], n[self.header_bytes:]
            except BlockingIOError:
                if len(self.send_burst.data) > 0:
                    break
                else:
                    return
            except Exception as e:
                self.logger.error(f"Error receiving data: {e}")
                return

            if len(n) > 0:
                self.send_burst.header.extend(header)
                self.send_burst.data.extend(data)
            else:
                return

            if len(self.send_burst.data) >= self.batch_size:
                burst_out = copy.deepcopy(self.send_burst)
                op_output.emit(burst_out.data, "burst_out")
                self.logger.debug(f"Emitting burst of size {len(burst_out.data)}")
                self.send_burst.reset()
                return


class PacketFormatterOp(Operator):
    """Format data from packets into a CuPy array and emit downstream"""
    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.sample_rate_in = float(fragment.kwargs("sensor")["sample_rate"])
        self.logger = setup_logging(self.name)
        self.prev_log_time = None
        self.bytes_sent = 0

    def setup(self, spec: OperatorSpec):
        spec.param("log_period")
        spec.input("burst_in")    # bytearray
        spec.output("signal_out") # configurable

    def _periodic_logging(self, nbytes: int):
        """Log ingest bandwidth every `self.log_period` seconds"""
        self.bytes_sent += nbytes
        tnow = time.time()
        if not self.prev_log_time:
            self.prev_log_time = tnow
            return

        dt = tnow - self.prev_log_time
        if dt > self.log_period:
            bw = self.bytes_sent / dt / 1e6
            self.logger.info(f"Ingest bandwidth {bw:.2f} MB/s")
            self.bytes_sent = 0
            self.prev_log_time = tnow

    def compute(self, op_input, op_output, context):
        """Just copy data to a GPU CuPy array and emit"""
        burst_in = op_input.receive("burst_in")
        self.logger.debug(f"Received burst of size {len(burst_in)}")
        data = cp.frombuffer(burst_in, dtype=cp.complex64)
        self.metadata["sample_rate"] = self.sample_rate_in
        self.logger.debug(f"Emitting signal of size {data.shape}")
        op_output.emit(data, "signal_out")

        # Log emission every N seconds
        self._periodic_logging(len(burst_in))


class ChannelizerOp(Operator):
    """Frequency shift a baseband IQ signal to multiple channels

    Takes a 1D CuPy array and creates a 2D tensor where each column represents
    the input signal shifted to a different frequency channel.
    """

    @classmethod
    def _jit_compile(cls):
        # JIT compile frequency shifting operations
        signal_len = 100_000
        test_signal = cp.ones(signal_len, dtype=cp.complex64)
        test_shifts = cp.ones((signal_len, 5), dtype=cp.complex64)
        _ = test_signal[:, cp.newaxis] * test_shifts

    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)
        self.sample_rate_in = float(fragment.kwargs("sensor")["sample_rate"])
        self.freq_shifts = None

        # JIT compile frequency shifting
        self.logger.info("Doing JIT compilation")
        ChannelizerOp._jit_compile()

    def setup(self, spec: OperatorSpec):
        spec.param("num_channels")
        spec.param("channel_spacing")
        spec.input("signal_in")
        spec.output("signal_out")

    def initialize(self):
        Operator.initialize(self)
        self.num_channels = int(self.num_channels)
        self.channel_spacing = float(self.channel_spacing)

    def _generate_frequency_shifts(self, signal_length):
        """Generate frequency shift vectors for each channel"""
        if self.sample_rate_in is None:
            self.logger.warning("Sample rate is not set, skipping frequency shift generation")
            return

        # Create frequency offsets centered around 0
        # For odd num_channels: [..., -2*spacing, -spacing, 0, +spacing, +2*spacing, ...]
        # For even num_channels: [..., -1.5*spacing, -0.5*spacing, +0.5*spacing, +1.5*spacing, ...]
        channel_indices = cp.arange(self.num_channels) - (self.num_channels - 1) / 2
        freq_offsets = channel_indices * self.channel_spacing

        self.logger.info(f"Frequency offsets (Hz): {cp.asnumpy(freq_offsets)}")

        # Generate time vector
        dt = 1.0 / self.sample_rate_in
        t = cp.arange(signal_length, dtype=cp.float32) * dt

        # Generate complex exponentials for frequency shifting
        # exp(-j*2*pi*f*t) shifts frequency by +f Hz
        self.freq_shifts = cp.zeros((signal_length, self.num_channels), dtype=cp.complex64)
        for i, freq_offset in enumerate(freq_offsets):
            self.freq_shifts[:, i] = cp.exp(-1j * 2 * cp.pi * freq_offset * t).astype(cp.complex64)

    def compute(self, op_input, op_output, context):
        signal_in = op_input.receive("signal_in")
        self.logger.debug(f"Received signal of size {signal_in.shape}")

        # Check if sample rate changed or if we need to regenerate shifts for new signal length
        if (
            self.sample_rate_in != self.metadata["sample_rate"]
            or self.freq_shifts is None
            or self.freq_shifts.shape[0] < signal_in.shape[0]
        ):
            self.sample_rate_in = self.metadata["sample_rate"]
            self._generate_frequency_shifts(signal_in.shape[0])

        # Apply frequency shifts to create multi-channel output
        # signal_in is 1D (N,), freq_shifts is 2D (N, num_channels)
        # Result is 2D (N, num_channels) where each column is a frequency-shifted version
        signal_out = signal_in[:, cp.newaxis] * self.freq_shifts[:signal_in.shape[0], :]

        # Pass through metadata
        self.metadata["sample_rate"] = self.sample_rate_in
        self.metadata["num_channels"] = self.num_channels
        op_output.emit(signal_out, "signal_out")
        self.logger.debug(f"Emitted signal of size {signal_out.shape}")


class LowPassFilterOp(Operator):
    """ Design and apply an FIR lowpass filter using a Hamming window.
    """
    @classmethod
    def _jit_compile(cls, numtaps, cutoff, fs):
        taps = cusignal.firwin(numtaps, cutoff=cutoff, window="hamming", fs=fs)
        lowpass(taps, cp.ones(1000, dtype=cp.complex64))

    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)
        self.channel_index = kwargs.get("channel_index", 0)

        # JIT compile of filter
        self.logger.info("Doing JIT compilation")
        self.sample_rate_in = float(fragment.kwargs("sensor")["sample_rate"])
        LowPassFilterOp._jit_compile(
            int(kwargs["numtaps"]), float(kwargs["cutoff"]), self.sample_rate_in
        )

    def setup(self, spec: OperatorSpec):
        spec.param("cutoff")
        spec.param("numtaps")
        spec.param("channel_index")
        spec.input("signal_in")
        spec.output("signal_out")

    def initialize(self):
        #todo: We could calculate the num taps on the metadata sample rate with caching
        Operator.initialize(self)
        self.cutoff = float(self.cutoff)
        self.numtaps = int(self.numtaps)
        self.channel_index = int(self.channel_index)
        self.taps = cusignal.firwin(
            self.numtaps, cutoff=self.cutoff, window="hamming", fs=self.sample_rate_in
        )

    def compute(self, op_input, op_output, context):
        signal_in = op_input.receive("signal_in")
        self.logger.debug(f"Received signal of size {signal_in.shape} on channel {self.channel_index}")

        # Extract the specific channel from multi-channel input
        channel_signal = extract_channel_signal(signal_in, self.channel_index, self.logger)
        if channel_signal is None:
            return

        signal_out = lowpass(self.taps, channel_signal)

        # Pass through metadata with channel info
        self.metadata["channel_id"] = self.channel_index
        op_output.emit(signal_out, "signal_out")
        self.logger.debug(f"Emitted signal of size {signal_out.shape} on channel {self.channel_index}")


class DemodulateOp(Operator):
    """ Do FM demodulation using discrete time differentiator
    """
    @classmethod
    def _jit_compile(cls):
        fm_demod(cp.ones(1000, dtype=cp.complex64))

    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)
        self.channel_index = kwargs.get("channel_index", 0)

        # JIT compile of demodulation
        self.logger.info("Doing JIT compilation")
        DemodulateOp._jit_compile()

    def setup(self, spec: OperatorSpec):
        spec.param("channel_index")
        spec.input("signal_in")
        spec.output("signal_out")

    def initialize(self):
        Operator.initialize(self)
        self.channel_index = int(self.channel_index)

    def compute(self, op_input, op_output, context):
        signal_in = op_input.receive("signal_in")
        self.logger.debug(f"Received signal of size {signal_in.shape} on channel {self.channel_index}")

        # Extract the specific channel from multi-channel input
        channel_signal = extract_channel_signal(signal_in, self.channel_index, self.logger)
        if channel_signal is None:
            return

        signal_out = fm_demod(channel_signal)

        # Pass through metadata with channel info
        self.metadata["channel_id"] = self.channel_index
        op_output.emit(signal_out, "signal_out")
        self.logger.debug(f"Emitted signal of size {signal_out.shape} on channel {self.channel_index}")

class ResampleOp(Operator):
    """ Up-sample or down-sample signal based on input
    """
    @classmethod
    def _jit_compile(cls):
        t_sig = cp.ones([int(1024*250e3//16e3)], dtype=cp.float32)
        cusignal.resample_poly(t_sig, 1, 2, window="hamming")

    def __init__(self, fragment, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)
        self.channel_index = kwargs.get("channel_index", 0)

        # JIT compile resampling
        self.logger.info("Doing JIT compilation")
        ResampleOp._jit_compile()

    def setup(self, spec: OperatorSpec):
        spec.param("sample_rate_out")
        spec.param("gain")
        spec.param("channel_index")
        spec.input("signal_in")
        spec.output("signal_out")

    def initialize(self):
        Operator.initialize(self)
        self.up, self.down = None, None
        self.sample_rate_in = None
        self.sample_rate_out = float(self.sample_rate_out)
        self.channel_index = int(self.channel_index)

    def _set_scaling(self):
        fs_small = min(self.sample_rate_in, self.sample_rate_out)
        fs_large = max(self.sample_rate_in, self.sample_rate_out)
        self.up, self.down = reduce_fraction(fs_large, fs_small)

    def _resample(self, data):
        if self.up == self.down:
            return data
        return cusignal.resample_poly(
            data, self.up, self.down, window="hamming"
        ).astype(cp.float32)

    def compute(self, op_input, op_output, context):
        signal_in = op_input.receive("signal_in")
        self.logger.debug(f"Received signal of size {signal_in.shape} on channel {self.channel_index}")

        # Extract the specific channel from multi-channel input
        channel_signal = extract_channel_signal(signal_in, self.channel_index, self.logger)
        if channel_signal is None:
            return

        # Check if up/down conversion needs to be re-computed
        if self.sample_rate_in != self.metadata["sample_rate"]:
            self.logger.info(f"type = {type(self.metadata['sample_rate'])}, {self.metadata['sample_rate']}")
            self.sample_rate_in = self.metadata["sample_rate"]
            self._set_scaling()

        # Do work and emit
        signal_out = self.gain * self._resample(channel_signal)
        self.metadata["sample_rate"] = self.sample_rate_out
        self.metadata["channel_id"] = self.channel_index
        op_output.emit(signal_out, "signal_out")
        self.logger.debug(f"Emitted signal of size {signal_out.shape} on channel {self.channel_index}")


class PcmToAsrOp(Operator):
    """
    Converts signal from float to PCM16 format, and moves it to host for processing by Riva.
    A seperate running thread that is reading the same shared buffer picks up the data and
    sends to Riva.
    """
    riva_fs = 16000           # sample rate for Riva (Hz)
    buffer_limit = 2*riva_fs  # 1 second of data for 16-bit PCM
    def __init__(self, fragment, shared_pcm_buffer, *args, **kwargs):
        super().__init__(fragment, *args, **kwargs)
        self.logger = setup_logging(self.name)
        self.shared_pcm_buffer = shared_pcm_buffer
        self.pcm_bytes = bytes()
        self.channel_index = kwargs.get("channel_index", 0)

    def setup(self, spec: OperatorSpec):
        spec.param("channel_index")
        spec.input("signal_in")

    def initialize(self):
        Operator.initialize(self)
        self.channel_index = int(self.channel_index)

    def compute(self, op_input, op_output, context):
        signal_in = op_input.receive("signal_in")
        self.logger.debug(f"Received signal of size {signal_in.shape} on channel {self.channel_index}")

        # Extract the specific channel from multi-channel input
        channel_signal = extract_channel_signal(signal_in, self.channel_index, self.logger)
        if channel_signal is None:
            return

        # Put 16-bit PCM byte array on shared Riva buffer
        pcm_data = float_to_pcm(channel_signal, cp.int16)
        self.pcm_bytes += cp.asnumpy(pcm_data).tobytes()
        if len(self.pcm_bytes) < self.buffer_limit:
            self.logger.debug(f"Not enough data to put on shared buffer, {len(self.pcm_bytes)} < {self.buffer_limit}")
            return

        # Check queue size before putting data
        queue_size_before = self.shared_pcm_buffer.qsize()

        self.shared_pcm_buffer.put(self.pcm_bytes)

        # Monitor queue size and warn about backpressure
        queue_size_after = self.shared_pcm_buffer.qsize()
        self.logger.debug(f"Put {len(self.pcm_bytes)} bytes on shared buffer (queue size: {queue_size_before} → {queue_size_after})")

        # Warn about potential backpressure
        if queue_size_after > 10:
            self.logger.warning(f"Queue backpressure detected on channel {self.channel_index}: {queue_size_after} items in queue")
        elif queue_size_after > 5:
            self.logger.info(f"Queue growing on channel {self.channel_index}: {queue_size_after} items")

        self.pcm_bytes = bytes()