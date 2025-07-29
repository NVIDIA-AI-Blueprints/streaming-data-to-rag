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
Entrypoint for running SDR Holoscan pipeline
Ingests UDP packets with baseband I/Q data
"""
import logging

from holoscan.core import Application, MetadataPolicy
from holoscan.schedulers import EventBasedScheduler
from queue import Queue

from common import (
    PARAM_FILE,
    FRONTEND_URI,
    DATABASE_URI,
    ASR_URI,
    wait_for_uri,
    setup_logging
)
from riva_asr import RivaThread
import operators as op

class AsrStreamingApp(Application):
    def __init__(self):
        super().__init__()
        self.logger = setup_logging(__name__)
        # Create separate PCM buffers for each channel
        self.num_channels = None  # Will be set from config
        self.pcm_buffers = {}  # Dictionary of channel_id -> Queue
        self.metadata_policy = MetadataPolicy.UPDATE

    def compose(self):
        self.logger.info("Composing SDR application")
        self.scheduler(EventBasedScheduler(
            self,
            name="event-based-scheduler",
            worker_thread_number=(self.num_channels+1),
            stop_on_deadlock_timeout=500
        ))

        # Data ingest operators (shared by all channels)
        network_rx = op.BasicNetworkRxOp(self, name="network_rx", **self.kwargs("network_rx"))
        pkt_format = op.PacketFormatterOp(self, name="pkt_format", **self.kwargs("pkt_format"))

        # Multi-channel frequency shift operator
        channelizer = op.ChannelizerOp(self, name="channelizer", **self.kwargs("channelizer"))

        # Create per-channel processing chains
        channel_operators = {}
        for channel_idx in range(self.num_channels):
            self.logger.info(f"Creating channel {channel_idx} operators")

            # Signal processing operators for this channel
            lowpassfilt = op.LowPassFilterOp(
                self,
                name=f"lowpassfilt_ch{channel_idx}",
                channel_index=channel_idx,
                **self.kwargs("lowpassfilt")
            )
            demodulate = op.DemodulateOp(
                self,
                name=f"demodulate_ch{channel_idx}",
                channel_index=channel_idx
            )
            resample = op.ResampleOp(
                self,
                name=f"resample_ch{channel_idx}",
                channel_index=channel_idx,
                **self.kwargs("resample")
            )
            pcm_to_asr = op.PcmToAsrOp(
                self,
                self.pcm_buffers[channel_idx],
                name=f"pcm_to_asr_ch{channel_idx}",
                channel_index=channel_idx
            )

            # Store operators for this channel
            channel_operators[channel_idx] = {
                'lowpassfilt': lowpassfilt,
                'demodulate': demodulate,
                'resample': resample,
                'pcm_to_asr': pcm_to_asr
            }

        # Application flow - shared pipeline up to frequency shift
        self.add_flow(network_rx, pkt_format, {("burst_out", "burst_in")})
        self.add_flow(pkt_format, channelizer, {("signal_out", "signal_in")})

        # Per-channel processing flows
        for channel_idx in range(self.num_channels):
            ops = channel_operators[channel_idx]

            # Connect channelizer to each channel's lowpass filter
            self.add_flow(channelizer, ops['lowpassfilt'], {("signal_out", "signal_in")})

            # Connect the processing chain for this channel
            self.add_flow(ops['lowpassfilt'], ops['demodulate'], {("signal_out", "signal_in")})
            self.add_flow(ops['demodulate'], ops['resample'], {("signal_out", "signal_in")})
            self.add_flow(ops['resample'], ops['pcm_to_asr'], {("signal_out", "signal_in")})

        self.logger.info("Done composing SDR application")

    def run(self):
        self.logger.info("Running SDR application")

        # Get number of channels from config
        self.num_channels = int(self.kwargs("channelizer")["num_channels"])

        # Create PCM buffers for each channel
        for channel_idx in range(self.num_channels):
            self.pcm_buffers[channel_idx] = Queue()

        # Wait for connections
        wait_for_uri(ASR_URI)
        wait_for_uri(FRONTEND_URI)
        wait_for_uri(DATABASE_URI)
        self.logger.info("All required services are ready")

        # Start Riva threads for each channel
        self.riva_handlers = {}
        for channel_idx in range(self.num_channels):
            self.logger.info(f"Starting Riva thread for channel {channel_idx}")
            self.riva_handlers[channel_idx] = RivaThread(
                self.pcm_buffers[channel_idx],
                self.kwargs("riva"),
                channel_id=channel_idx,
                asr_uri=ASR_URI,
                frontend_uri=FRONTEND_URI,
                database_uri=DATABASE_URI,
                initialize=(channel_idx == 0)
            )
            self.riva_handlers[channel_idx].start()

        # Run application
        self.logger.info("Starting application")
        super().run()

        # Stop all Riva threads
        for channel_idx in range(self.num_channels):
            self.riva_handlers[channel_idx].stop()
            self.riva_handlers[channel_idx].join()

if __name__ == "__main__":
    app = AsrStreamingApp()
    app.config(PARAM_FILE)
    app.run()