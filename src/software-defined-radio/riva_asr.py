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

import os
import threading
import time
import json
import requests
import uuid

import riva.client
import riva.client.proto.riva_asr_pb2 as rasr

from queue import Queue
from queue import Empty as QueueEmptyException
from copy import deepcopy
from datetime import datetime, timezone
from common import setup_logging


class RivaThread(threading.Thread):
    # Class-level counter and lock for thread-safe document ID generation
    _global_doc_id_counter = 0
    _doc_id_lock = threading.Lock()

    def __init__(
        self,
        buffer: Queue,
        params,
        asr_uri=None,
        frontend_uri=None,
        database_uri=None,
        channel_id=None,
        initialize=True
    ):
        threading.Thread.__init__(self)
        self.buffer = buffer
        self.params = params
        self.asr_uri = asr_uri
        self.frontend_uri = frontend_uri
        self.database_uri = database_uri
        self.channel_id = channel_id if channel_id is not None else 0
        self._database_text = None
        self._min_db_export_chars = self.params["min_db_export_chars"]

        # Setup channel-aware logger
        logger_name = f"riva_asr_ch{self.channel_id}" if channel_id is not None else "riva_asr"
        self.logger = setup_logging(logger_name)

        self._prev_partial_transcript = None
        self._buffer_get_timeout = 30  # (sec) timeout for waiting on new buffer entries
        self.collection_name = "RadioStream"

        # Timing tracking for NTP timestamps
        self._first_transcript_time = None
        self._prev_export_time = None

        # Riva handlers
        self._setup_riva()
        self._kill = threading.Event()

        # Initialize collection
        if self.database_uri is not None and initialize:
            self._initialize_ingest_service()

        self.logger.info(f"RivaThread initialized for channel {self.channel_id}")

    @classmethod
    def get_next_doc_id(cls):
        """Thread-safe method to get the next document ID"""
        with cls._doc_id_lock:
            cls._global_doc_id_counter += 1
            return cls._global_doc_id_counter - 1  # Return the value before increment

    def _setup_riva(self):
        self._riva_auth = riva.client.Auth(uri=self.asr_uri)
        self._riva_client = riva.client.ASRService(self._riva_auth)
        self._riva_config = self._gen_config()

    def _initialize_ingest_service(self):
        """Initialize the context-aware RAG ingestion service"""
        try:
            # Get UUID from environment variable, fallback to default
            self._rag_uuid = os.environ.get("RAG_UUID", "123456")
            response = requests.post(
                f"http://{self.database_uri}/init",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"uuid": self._rag_uuid}),
                timeout=10
            )
            assert response.status_code == 200, "Failed to initialize ingestion service"
            self.logger.info(f"Ingestion service initialized with UUID: {self._rag_uuid}")
        except requests.exceptions.ConnectionError as e:
            self.logger.error(f"Failed to connect to ingestion service at {self.database_uri}: {str(e)}")
            raise
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error during ingestion service initialization: {str(e)}")
            raise
        except AssertionError as e:
            self.logger.error(f"Failed to initialize ingestion service: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error during ingestion service initialization: {str(e)}")
            raise

    def run(self):
        while not self._kill.is_set():
            try:
                responses = self.make_riva_request()
                self.extract_transcripts(responses)
            except Exception as e:
                if self._kill.is_set():
                    self.logger.info(f"Riva thread for channel {self.channel_id} stopped gracefully")
                    break
                self.logger.error(f"Riva thread exception on channel {self.channel_id}")
                self.logger.error(f"Waiting 5 seconds then trying again...")
                time.sleep(5)
                self._setup_riva()
                continue

        self.logger.info(f"Riva thread for channel {self.channel_id} exiting")

    def stop(self):
        self.logger.info(f"Stopping Riva thread for channel {self.channel_id}")
        self._kill.set()

    def _datetime_to_ntp_formats(self, dt):
        """Convert datetime to both NTP string and float formats"""
        # Ensure datetime is timezone-aware (UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        ntp_string = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3] + "Z"
        ntp_float = dt.timestamp()
        return ntp_string, ntp_float

    def _database_export(self, transcript):
        if not self.database_uri:
            return

        # Append to database text and export only if long enough
        if self._database_text is None:
            self._database_text = transcript
        else:
            self._database_text += f" {transcript}"

        if len(self._database_text) < self._min_db_export_chars:
            return

        # Determine start and end times for this export
        start_time = self._prev_export_time if self._prev_export_time is not None else self._first_transcript_time
        start_ntp, start_ntp_float = self._datetime_to_ntp_formats(start_time)
        end_time = datetime.now(timezone.utc)
        end_ntp, end_ntp_float = self._datetime_to_ntp_formats(end_time)

        # Export to database
        self.logger.info(f"Database export (Channel {self.channel_id}): {self._database_text}")
        timestamp = end_time.strftime("%Y-%m-%d %H:%M:%S")
        doc_index = self.get_next_doc_id()
        data = {
            'document': self._database_text,
            "doc_index": doc_index,
            "doc_metadata": {
                "is_first": self._prev_export_time is None,
                "is_last": False,
                "file": f"rtsp://fm-radio-ch{self.channel_id}",
                "streamId": f"fm-radio-ch{self.channel_id}",
                "doc_id": f"fm-radio-ch{self.channel_id}",
                "chunkIdx": doc_index,
                "timestamp": timestamp,
                "start_ntp": start_ntp,
                "end_ntp": end_ntp,
                "start_ntp_float": start_ntp_float,
                "end_ntp_float": end_ntp_float,
                "start_pts": int(start_ntp_float * 1e9),  # Convert to nanoseconds
                "end_pts": int(end_ntp_float * 1e9),
                "uuid": str(uuid.uuid4())
            }
        }
        self.logger.debug(f"Database export (Channel {self.channel_id}) done")

        endpoint = f'http://{self.database_uri}/add_doc'
        try:
            response = requests.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                data=json.dumps(data),
                timeout=10
            )
            response.raise_for_status()
            if response.status_code != 200:
                self.logger.error(f"Failed to add document. Status: {response.status_code}")
            else:
                self.logger.info(f"Document added to ingestion service: {response.json()['status']}")
        except requests.exceptions.ConnectionError:
            self.logger.error(f"Failed to connect to the '{endpoint}' endpoint")
        except Exception as e:
            self.logger.error(f"Error posting ingest request: {e}")

        # Update timing state immediately (don't wait for response)
        self._database_text = None
        self._first_transcript_time = None
        self._prev_export_time = end_time

    def _frontend_export(self, transcript, timestamp=None, uuid=None):
        self.logger.info(f"Frontend export (Channel {self.channel_id}): {transcript}")
        if not self.frontend_uri:
            return
        elif timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        endpoint = f"http://{self.frontend_uri}/api/update-text"
        data = {
            "text": transcript,
            "stream_id": f"fm-radio-ch{self.channel_id}",
            "timestamp": timestamp,
            "finalized": False,
            "uuid": uuid
        }
        try:
            client_response = requests.post(endpoint, json=data, timeout=3)
            self.logger.debug(f'Posted {data}, got response {client_response._content}')
            self.logger.debug("--------------------------")
        except requests.exceptions.ConnectionError:
            self.logger.error(f"Failed to connect to the '{endpoint}' endpoint")
        except Exception as e:
            self.logger.error(f"Error posting request with {endpoint}: {e}")

    def _export_final_transcript(self, transcript):
        """ Send final transcript to both database and frontend
        """
        self.logger.debug(f"Final (Channel {self.channel_id}): {transcript}")
        self._database_export(transcript)

    def _export_partial_transcript(self, transcript):
        """ Update frontend with partial transcript
        """
        if transcript == self._prev_partial_transcript:
            return

        self._prev_partial_transcript = transcript
        self.logger.debug(f"Partial (Channel {self.channel_id}): {transcript}")
        self._frontend_export(transcript)

    def extract_transcripts(self, responses):
        if not responses:
            self.logger.debug(f"No responses for channel {self.channel_id}")
            return

        for response in responses:
            if self._kill.is_set():
                self.logger.info(f"Riva thread killed for channel {self.channel_id}")
                break
            if not response.results:
                self.logger.debug(f"No results for channel {self.channel_id}")
                continue

            if self._first_transcript_time is None:
                self._first_transcript_time = datetime.now(timezone.utc)

            is_final = False
            partial_transcript = ""
            for result in response.results:
                # Note: this assumes max_alternatives == 1
                transcript = result.alternatives[0].transcript
                if len(transcript) == 0:
                    self.logger.debug(f"Empty transcript for channel {self.channel_id}")
                    continue

                if result.is_final:
                    is_final = True
                    self._export_final_transcript(transcript)
                else:
                    partial_transcript += transcript

            if not is_final:
                self._export_partial_transcript(partial_transcript)

    def _gen_config(self) -> riva.client.StreamingRecognitionConfig:
        asr_config = riva.client.RecognitionConfig(
                encoding=riva.client.AudioEncoding.LINEAR_PCM,
                language_code=self.params["src_lang_code"],
                max_alternatives=1,
                profanity_filter=False,
                enable_automatic_punctuation=self.params["automatic_punctuation"],
                verbatim_transcripts=self.params["verbatim_transcripts"],
                sample_rate_hertz=self.params["sample_rate"],
                audio_channel_count=1
            )
        streaming_config = riva.client.StreamingRecognitionConfig(
            config=deepcopy(asr_config),
            interim_results=True
        )
        return streaming_config

    def _request_generator(self):
        yield rasr.StreamingRecognizeRequest(streaming_config=self._riva_config)
        while not self._kill.is_set():
            try:
                yield rasr.StreamingRecognizeRequest(
                    audio_content=self.buffer.get(timeout=self._buffer_get_timeout)
                )
            except QueueEmptyException:
                # Timeout reached. If there is no timeout, the Riva gRPC connection
                # seems to 'forget' about the StreamingRecognizeRequest and throws an
                # error because the first request doesn't specify the START flag.
                break
            except Exception as e:
                if self._kill.is_set():
                    self.logger.debug(f"Request generator interrupted for channel {self.channel_id}")
                    break
                else:
                    raise
        self.logger.info(f"Exiting _request_generator for channel {self.channel_id}")

    def make_riva_request(self):
        self.logger.debug(f"Making Riva request for channel {self.channel_id}")
        responses = self._riva_client.stub.StreamingRecognize(
            self._request_generator(),
            metadata=self._riva_client.auth.get_auth_metadata()
        )
        return responses