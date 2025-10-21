# FM File Replay Service

The FM File Replay service simulates real Software-Defined Radio (SDR) hardware by reading audio files, performing GPU-accelerated FM modulation, and transmitting baseband I/Q samples via UDP to the Holoscan SDR pipeline.

## Overview

This service is **not** simply replaying audio files. Instead, it provides a complete simulation of the type of data that would be received from physical SDR hardware connected to FM radio channels. The replay service:

1. **Reads audio files** from the configured file list
2. **FM-modulates the audio data** to specified channel frequencies
3. **Sums across multiple simulated FM channels**
4. **Transmits the resulting I/Q samples** via UDP to the Holoscan SDR

This allows for realistic testing and development without requiring physical SDR hardware.

## Configuration

### Environment Variables

Replay files must be located within the build context of the `file-replay` service that is deployed, specifically inside `src/file-replay/files`.

So for a file located at:
```bash
streaming-data-to-rag/src/file-replay/files/file1.mp3
```

the ENV should be set to
```bash
export REPLAY_FILES="file1.mp3"
```

Configure the file replay service using these environment variables:

#### Required
```bash
export REPLAY_FILES="file1.mp3,file2.mp3,file3.mp3"  # Comma-separated list of audio files
```

#### Optional
```bash
export REPLAY_TIME=3600                    # Maximum replay time in seconds (default: 3600)
export REPLAY_MAX_FILE_SIZE=50            # Maximum size of individual file in MB (default: 50)
```

### File Requirements

- **Location**: All replay files must be placed in `src/file-replay/files/`
- **Formats**: Supported audio formats include MP3, WAV, and other common audio files
- **Size Limit**: Individual files should not exceed the configured `REPLAY_MAX_FILE_SIZE` (default: 50MB)
- **Naming**: Files should be named as specified in the `REPLAY_FILES` environment variable

### Holoscan SDR Parameter Alignment

The replay configuration must match the Holoscan SDR parameters in `src/software-defined-radio/params.yaml`:

| Parameter | Holoscan Config Path | Description |
|-----------|---------------------|-------------|
| Sample Rate | `sensor.sample_rate` | I/Q sample rate in Hz |
| UDP Port | `network_rx.dst_port` | UDP destination port |
| Max Payload Size | `network_rx.max_payload_size` | UDP packet size limit |
| Number of Channels | `channelizer.num_channels` | Number of FM channels to simulate |
| Channel Spacing | `channelizer.channel_spacing` | Frequency spacing between channels in Hz |

## Usage

### 1. Prepare Audio Files

Place your audio files in the `src/file-replay/files/` directory:

```bash
# Example file structure
src/file-replay/files/
├── news_broadcast.mp3
├── music_station.mp3
└── talk_radio.wav
```

### 2. Configure Environment

Set the required environment variables:

```bash
export REPLAY_FILES="news_broadcast.mp3,music_station.mp3,talk_radio.wav"
export REPLAY_TIME=7200  # 2 hours
export REPLAY_MAX_FILE_SIZE=100  # 100MB limit
```

### 3. Build and Deploy

Build the container as part of the main deployment:

```bash
docker compose -f deploy/docker-compose.yaml --profile replay build
```

Deploy the service:

```bash
docker compose -f deploy/docker-compose.yaml --profile replay up -d
```

### 4. Verify Operation

Check that the service is running and transmitting data:

```bash
# Check container status
docker logs fm-file-replay

# Verify UDP traffic (if netstat is available)
netstat -u | grep <UDP_PORT>
```

## Technical Details

### FM Modulation Process

1. **Audio Input**: Reads entire set of audio files and holds in memory
2. **Channel Assignment**: Each file is assigned to a specific FM channel frequency
3. **FM Modulation**: Audio is frequency-modulated using GPU acceleration
4. **Channel Summation**: All modulated channels are summed to create composite baseband signal
5. **I/Q Generation**: Complex baseband samples are generated for transmission
6. **UDP Transmission**: I/Q samples are packetized and sent via UDP

### Container Base

- **Base Image**: `nvcr.io/nvidia/pytorch:23.08-py3`
- **GPU Requirements**: CUDA-capable GPU for FM modulation acceleration
- **Dependencies**: PyTorch, audio processing libraries, network utilities

### Network Configuration

The service transmits UDP packets containing I/Q samples to the Holoscan SDR container. Ensure network connectivity between containers:

- **Protocol**: UDP
- **Data Format**: Complex I/Q samples (interleaved float32)
- **Packet Size**: Configurable via `max_payload_size`
- **Target**: Holoscan SDR container on specified port

## Troubleshooting

### Common Issues

**Files Not Found**
```bash
# Verify file location
ls -la src/file-replay/files/
# Check environment variable
echo $REPLAY_FILES
```

**UDP Transmission Issues**
```bash
# Check container networking
docker network ls
docker network inspect <network_name>
```

**Audio Format Issues**
```bash
# Check file formats
file src/file-replay/files/*.mp3
# Verify file sizes
du -h src/file-replay/files/*
```

### Logs and Debugging

View detailed service logs:

```bash
# Real-time logs
docker logs -f fm-file-replay

# Recent logs
docker logs --tail=100 fm-file-replay
```

### Performance Monitoring

Monitor GPU usage during FM modulation:

```bash
# Inside container or on host
nvidia-smi

# Container resource usage
docker stats fm-file-replay
```

## Integration with Physical SDR

When transitioning from file replay to physical SDR hardware:

1. **Disable file replay**: Remove `--profile replay` from docker compose commands
2. **Enable USB mounting**: Add USB device mounts to docker-compose.yaml
3. **Configure GNU Radio**: Use provided GRC file in `examples/` directory
4. **Update parameters**: Ensure sample rates and frequencies match your hardware

For detailed physical SDR setup, see the main README section on "USB Device Mounting for Physical SDR Hardware".
