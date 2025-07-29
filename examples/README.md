# Examples and Hardware Integration

This directory contains an example GNU Radio configuration file for integrating physical SDR hardware with the Streaming Data to RAG system.

## Contents

- **[gnuradio/](gnuradio/)** - GNU Radio Companion (GRC) files for real-time SDR signal processing
- **[USB Device Setup](#usb-device-mounting-for-physical-sdr-hardware)** - Guide for connecting physical SDR hardware

## USB Device Mounting for Physical SDR Hardware

By default, **no USB devices are mounted** into the containers. This ensures compatibility on systems where `/dev/bus/usb` does not exist or is not accessible.

### When to Enable USB Device Mounting

You only need to enable USB device mounting if you are using **physical SDR hardware** (like an RTL-SDR, HackRF, or similar USB-based software-defined radio) that needs to be accessed from within the containers.

### How to Enable USB Device Mounting

If you have physical SDR hardware connected via USB, you need to add the device mounts back to your `docker-compose.yaml` file.

**1. Edit `deploy/docker-compose.yaml`**

Add the following `devices` section to the `holoscan-sdr` service:

```yaml
  holoscan-sdr:
    # ... existing configuration ...
    devices:
      - "/dev/bus/usb:/dev/bus/usb"
```

**2. Ensure USB Device Permissions**

Make sure your user has access to the USB device:

```bash
# Add your user to the plugdev group
sudo usermod -a -G plugdev $USER

# Create udev rules for your SDR device (example for RTL-SDR)
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"' | sudo tee /etc/udev/rules.d/20-rtlsdr.rules

# Reload udev rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**3. Verify Device Access**

Check that your SDR device is accessible:

```bash
ls -la /dev/bus/usb/
```

You should see your SDR device listed.

**4. Run Docker Compose**

Start your services as usual (**without** replay service):

```bash
docker compose -f deploy/docker-compose.yaml up -d
```

## GNU Radio Integration

For users who have their own SDR hardware and want to use GNU Radio for signal processing, sample GNU Radio Companion (GRC) files are provided in the `gnuradio/` subdirectory.

### Key Features

- Tunes to an FM radio station
- Streams baseband I/Q samples to the Holoscan SDR via UDP
- Compatible with various SDR hardware (RTL-SDR, HackRF, USRP, etc.)
- Configurable frequency, bandwidth, sample rate, and UDP parameters

### Usage Instructions

1. **Open the GRC file** in GNU Radio Companion from the `gnuradio/` folder
2. **Configure your SDR source block** for your specific hardware:
   - RTL-SDR: Use `RTL-SDR Source` block
   - HackRF: Use `HackRF Source` block
   - USRP: Use `UHD: USRP Source` block
3. **Set the desired FM frequency** and sample rate for your local stations
4. **Ensure UDP sink parameters** match your Holoscan SDR configuration:
   - IP address: Target container IP or localhost
   - Port: Should match `network_rx.dst_port` in `src/software-defined-radio/params.yaml`
   - Payload size: Should match `network_rx.max_payload_size`
5. **Generate and run** the flowgraph

### Integration Benefits

This approach allows you to:
- Use your own SDR hardware instead of the file replay system
- Receive real-time FM radio signals
- Process live RF data through the Holoscan-based pipeline
- Maintain full compatibility with the existing transcription and RAG workflow

### Prerequisites

- GNU Radio installed on your system
- Physical SDR hardware (RTL-SDR, HackRF, USRP, etc.)
- USB device mounting enabled as detailed above
- Proper SDR drivers installed for your hardware

## Troubleshooting Hardware Integration

### Common Issues

**USB Device Not Found**
```bash
# Check if device is detected
lsusb | grep -i rtl  # For RTL-SDR
lsusb | grep -i hack # For HackRF

# Verify permissions
ls -la /dev/bus/usb/
```

**GNU Radio Flowgraph Issues**
```bash
# Check GNU Radio installation
gnuradio-config-info --version

# Verify SDR-specific modules
python3 -c "import osmosdr; print('osmosdr OK')"  # For RTL-SDR
python3 -c "import uhd; print('UHD OK')"          # For USRP
```

**Container Connectivity**
```bash
# Test UDP connectivity
nc -u <container_ip> <udp_port>

# Check Docker network
docker network inspect <network_name>
```

### Getting Help

- **SDR Hardware**: Consult your device's documentation for driver installation
- **GNU Radio**: Visit [GNU Radio's documentation](https://www.gnuradio.org/doc/)
- **Docker Issues**: Check the main README troubleshooting section

---

**Note:** If you encounter errors about missing `/dev/bus/usb`, ensure the device exists and you have proper permissions. Different SDR devices may require specific udev rules - consult your device's documentation for details.