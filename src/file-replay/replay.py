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

"""
Given a .wav audio file, this function will convert the floating-point
audio samples to I/Q FM-modulated samples and send them as UDP packets
downstream to the Holoscan SDR application. Note that the replay parameters
used here should match up with the expected parameters in the SDR's
params.yml file.
"""

import time
import os
import logging
import librosa
import argparse
import struct
import socket

import cupy as cp
import cupyx.scipy.signal as cusignal

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay audio file(s) as FM-modulated, baseband I/Q radio data via UDP"
    )
    parser.add_argument(
        "--dst-ip",
        type=str,
        default="0.0.0.0",
        help="IP address, should match 'network_rx.ip_addr' in sdr-holoscan/params.yml"
    )
    parser.add_argument(
        "--dst-port",
        type=int,
        default=5005,
        help="Destination port, should match 'network_rx.dst_port' in sdr-holoscan/params.yml"
    )
    parser.add_argument(
        "--file-names",
        type=str,
        nargs="*",
        default=[],
        help="Filenames, should be located in file-replay/files. Multiple files can be specified as separate arguments or comma-separated string."
    )
    parser.add_argument(
        "--freq-separation",
        type=float,
        default=200000,
        help="Frequency separation between channels in Hz (default: 200kHz)"
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=1e6,
        help="Output sample rate, should match 'sensor.sample_rate' in sdr-holoscan/params.yml"
    )
    parser.add_argument(
        "--packet-size",
        type=int,
        default=1472,
        help="Size in bytes of each UDP packet, plus 8 counting bytes at front"
    )
    parser.add_argument(
        "--init-time",
        type=float,
        default=30,
        help="Sleep time prior to starting, allows other containers to spin up."
    )
    parser.add_argument(
        "--total-time",
        type=float,
        default=0,
        help="Total runtime. If non-zero, loops until time is hit if .wav file is shorter."
    )
    parser.add_argument(
        "--max-file-size",
        type=float,
        default=50,
        help="Maximum file size in MB (default: 50MB)"
    )
    args = parser.parse_args()

    # Split the comma-separated argument into multiple filenames
    args.file_names = [name.strip() for name in args.file_names[0].split(',')]
    logger.info(f"Split comma-separated file names: {args.file_names}")

    return args

def wait_for_dst(dst_ip, dst_port, wait_time=5, timeout=300):
    """ Try to connect until successful
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((dst_ip, dst_port))
    start_time = time.time()
    curr_time = start_time
    while curr_time - start_time < timeout:
        try:
            sock.sendto(struct.pack('Q', 0), (dst_ip, dst_port))
            logger.info(f"{dst_ip}:{dst_port} open, replaying")
            return
        except ConnectionRefusedError:
            logger.info(f"Waiting {wait_time}s for {dst_ip}:{dst_port} to open")
            time.sleep(wait_time)
    logger.error(f"{dst_ip}:{dst_port} never opened")

def fm_modulate(audio, fs_in, fs_out, deviation=100000, freq_shift=0):
    """ Given audio samples in floating point, FM modulate and frequency shift"""
    # Resample
    nsamples = int(audio.shape[0] * fs_out / fs_in)
    chunk = cusignal.resample(audio, nsamples)

    # Integrate and frequency modulate
    integrated_audio = cp.cumsum(chunk) / fs_out
    phase_deviation = 2 * cp.pi * deviation * integrated_audio
    fm_samples = cp.cos(phase_deviation) + 1j*cp.sin(phase_deviation)

    # Apply frequency shift if specified
    if freq_shift != 0:
        t = cp.arange(len(fm_samples)) / fs_out
        freq_shift_samples = cp.exp(1j * 2 * cp.pi * freq_shift * t)
        fm_samples = fm_samples * freq_shift_samples

    return fm_samples.astype(cp.complex64)

def send_packet(sock, data, dst_ip, dst_port):
    try:
        sock.sendto(data, (dst_ip, dst_port))
        return None
    except Exception as e:
        logger.error(f"Failed to send packet: {e}")
        return e

def check_file_sizes(file_names, max_size_mb=50):
    """Check that all files are under the specified size limit"""
    max_size_bytes = max_size_mb * 1024 * 1024

    for file_name in file_names:
        file_path = os.path.join("files", file_name)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_size = os.path.getsize(file_path)
        if file_size > max_size_bytes:
            raise ValueError(f"File {file_name} is {file_size/1024/1024:.1f}MB, exceeds {max_size_mb}MB limit")

        logger.info(f"File {file_name}: {file_size/1024/1024:.1f}MB - OK")

def load_all_audio_files(file_names):
    """Load all audio files into memory"""
    audio_data = []
    file_info = []

    for file_name in file_names:
        file_path = os.path.join("files", file_name)
        try:
            # Load entire file
            audio, fs_in = librosa.load(file_path, sr=None)  # Keep original sample rate
            duration = len(audio) / fs_in

            audio_data.append(audio)
            file_info.append({
                'name': file_name,
                'fs_in': fs_in,
                'duration': duration,
                'samples': len(audio)
            })

            logger.info(f"Loaded {file_name}: {duration:.2f}s, {fs_in}Hz, {len(audio)} samples")

        except Exception as e:
            logger.error(f"Error loading {file_name}: {e}")
            raise e

    return audio_data, file_info

def check_bandwidth_requirements(num_files, freq_separation, fs_out, channel_bandwidth=200000):
    """
    Check if the sample rate is high enough to support the desired bandwidth.

    Args:
        num_files: Number of audio files to be transmitted
        freq_separation: Frequency separation between channels in Hz
        fs_out: Output sample rate in Hz
        channel_bandwidth: Estimated bandwidth per channel in Hz (default: 200kHz for FM audio)

    Raises:
        ValueError: If sample rate is insufficient
    """
    # Bandwidth check: sample rate > (num_files - 1) * freq_separation + channel_bandwidth
    required_bandwidth = (num_files - 1) * freq_separation + channel_bandwidth
    supported_bandwidth = fs_out / 2

    if supported_bandwidth < required_bandwidth:
        raise ValueError(
            f"Sample rate {fs_out/1e6:.1f} MHz is insufficient for {num_files} files with "
            f"{freq_separation/1e6:.1f} MHz separation.\n"
            f"Current sample rate: {fs_out/1e6:.1f} MHz (Nyquist: {supported_bandwidth/1e6:.1f} MHz)\n"
            f"Please increase sample rate to at least {2*required_bandwidth/1e6:.1f} MHz"
        )

    logger.info(f"Bandwidth check passed:")
    logger.info(f" - Channel span: ({num_files} - 1) x {freq_separation/1e6:.1f} MHz = {(num_files - 1) * freq_separation/1e6:.1f} MHz")
    logger.info(f" - Channel bandwidth: {channel_bandwidth/1e6:.1f} MHz")
    logger.info(f" - Total required: {required_bandwidth/1e6:.1f} MHz")
    logger.info(f" - Sample rate: {fs_out/1e6:.1f} MHz > {2*required_bandwidth/1e6:.1f} MHz")

def replay_multiple(file_names, fs_out, dst_ip, dst_port, pkt_size, freq_separation, max_file_size, chunk_time=2, total_time=0):
    """Replay multiple audio files as combined I/Q stream"""
    if not file_names:
        logger.error("No files provided")
        return

    # Check bandwidth requirements
    logger.info("Checking bandwidth requirements...")
    check_bandwidth_requirements(len(file_names), freq_separation, fs_out)

    # Check file sizes first
    logger.info("Checking file sizes...")
    check_file_sizes(file_names, max_file_size)

    # Load all audio files into memory (raw audio only)
    logger.info("Loading all audio files into memory...")
    audio_data, file_info = load_all_audio_files(file_names)

    if not audio_data:
        logger.error("Failed to load any files")
        return

    # Calculate frequency offsets for each file, always putting one channel at 0 Hz
    num_files = len(audio_data)
    freq_offsets = [0]  # First file at baseband

    # Add remaining files alternating +/- around baseband
    step = 1
    for i in range(1, num_files):
        if i % 2 == 1:  # Odd positions: +freq_separation, +2*freq_separation, etc.
            freq_offsets.append(step * freq_separation)
        else:           # Even positions: -freq_separation, -2*freq_separation, etc.
            freq_offsets.append(-step * freq_separation)
            step += 1   # Increment step after each negative frequency

    freq_offsets.sort()
    logger.info(f"Processing {num_files} files with frequencies: {[f/1e6 for f in freq_offsets]} MHz")

    # Setup socket
    logger.info(f"Setting up UDP socket to {dst_ip}:{dst_port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Determine total time
    max_duration = max(info['duration'] for info in file_info)
    if not total_time:
        total_time = max_duration

    logger.info(f"Starting multi-file replay for {total_time:.2f}s (longest file: {max_duration:.2f}s)")

    # Calculate chunk size in samples for each file's original sample rate
    chunk_sizes = [int(chunk_time * info['fs_in']) for info in file_info]

    # Stream the combined signal
    elapsed = 0
    chunk_positions = [0] * num_files  # Track position in each file

    while elapsed < total_time:
        # Reset stats
        start_time = time.time()
        prev_time = start_time
        pkts_sent = 0
        bytes_sent = 0

        # Load and modulate chunks from all active files
        modulated_chunks = []
        active_files = 0
        max_samples_out = 0

        for i, (audio, info) in enumerate(zip(audio_data, file_info)):
            start_pos = chunk_positions[i]
            end_pos = start_pos + chunk_sizes[i]

            if start_pos < len(audio):
                active_files += 1
                # Extract chunk from this file
                audio_chunk = audio[start_pos:min(end_pos, len(audio))]

                if len(audio_chunk) > 0:
                    # FM modulate this chunk with frequency shift
                    samples = fm_modulate(cp.array(audio_chunk), info['fs_in'], fs_out, freq_shift=freq_offsets[i])
                    modulated_chunks.append(samples)

                    if len(samples) > max_samples_out:
                        max_samples_out = len(samples)

                # Update position for next chunk
                chunk_positions[i] = end_pos
            else:
                # This file is finished, add empty chunk
                modulated_chunks.append(None)

        if active_files == 0:
            logger.info("All files finished")
            break

        # Combine all modulated chunks
        combined_signal = cp.zeros(max_samples_out, dtype=cp.complex64)
        for chunk in modulated_chunks:
            if chunk is not None:
                if len(chunk) < max_samples_out:
                    # Zero pad shorter chunks
                    padded_chunk = cp.zeros(max_samples_out, dtype=cp.complex64)
                    padded_chunk[:len(chunk)] = chunk
                    combined_signal += padded_chunk
                else:
                    combined_signal += chunk

        # Convert to bytes and send
        iq_data = combined_signal.tobytes()
        inter_pkt_time = chunk_time / (len(iq_data) // pkt_size)

        # Form packet and send
        for i in range(0, len(iq_data), pkt_size):
            # Send
            header = struct.pack('Q', pkts_sent)
            pkt_data = iq_data[i:i+pkt_size]
            result = send_packet(sock, header + pkt_data, dst_ip, dst_port)
            while result is ConnectionRefusedError:
                logger.info(f"Connection refused, sleeping 5s")
                time.sleep(5)
                result = send_packet(sock, header + pkt_data, dst_ip, dst_port)

            pkts_sent += 1
            bytes_sent += len(pkt_data)

            # Wait allotted time
            curr_time = time.time()
            while (curr_time - prev_time) < inter_pkt_time:
                curr_time = time.time()
            prev_time = curr_time

        # Print stats
        dt = curr_time - start_time
        elapsed += dt
        logger.info(f"Multi-file stats ({elapsed:.2f}s):")
        logger.info(f" - {pkts_sent} packets")
        logger.info(f" - {bytes_sent} bytes")
        logger.info(f" - {bytes_sent / dt / 1e6:.2f} MB/s")
        logger.info(f" - Active files: {active_files}")

        if elapsed >= total_time:
            break

if __name__ == "__main__":
    args = parse_args()
    if not args.file_names:
        logger.info("No files provided, exiting")
        exit()

    # Wait for other apps
    logger.info(f"Sleeping {args.init_time}s to allow time for SDR to spin up")
    time.sleep(args.init_time)
    wait_for_dst(args.dst_ip, args.dst_port)

    # Use multi-file replay for both single and multiple files
    replay_multiple(
        args.file_names,
        args.sample_rate,
        args.dst_ip,
        args.dst_port,
        args.packet_size,
        args.freq_separation,
        args.max_file_size,
        total_time=args.total_time
    )