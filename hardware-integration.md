## Hardware Integration

For users who want to use **physical SDR hardware** instead of the file replay system, setup guides and examples are available in the [examples](examples/) directory.

### Available Resources

- **[USB Device Mounting Guide](examples/README.md#usb-device-mounting-for-physical-sdr-hardware)** - Instructions for connecting physical SDR hardware (RTL-SDR, HackRF, USRP, etc.) to the containerized system
- **[GNU Radio Integration](examples/README.md#gnu-radio-integration)** - Sample GNU Radio Companion (GRC) file and setup instructions for real-time signal processing

### Quick Start with Physical Hardware

1. **Follow the USB setup guide** in [examples/README.md](examples/README.md) to configure device mounting and permissions
2. **Choose your approach**:
   - Use provided GNU Radio examples for custom signal processing
   - Modify the existing Holoscan pipeline for direct hardware integration
3. **Configure parameters** to match your hardware specifications and local FM frequencies

This approach enables real-time FM radio reception and processing while maintaining full compatibility with the existing transcription and RAG workflow.
