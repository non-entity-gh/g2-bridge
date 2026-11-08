# Goggles 2 PC video protocol

This protocol displayed video on a stock DJI Goggles 2 with firmware 01.11.00.00 and 01.14.00.00. The control exchange is byte-identical on both. It adapts the Android Virtual Flight application's video metadata to the PC USB interface.

## Source artifacts

REA inventoried the Android APK, JADX exposed the accessory and native entry points, and Ghidra analysis recovered the H.264 send chain. ARM emulation checked serializer and video-fragment output against the [native fixtures](../tests/fixtures).

The analysis used these artifacts:

| Artifact | SHA-256 |
| --- | --- |
| Original APK | `e6181ea1f5e1dab39f67713c647d1c175de614019b90252a082fd8f2bd122fc2` |
| ARM32 `libUE4.so` | `c75d099fa9b7a4962909171808857a51c4ef88a02f21e9ae40c1e05924c66df7` |
| x64 DJI `libusb0_device.dll` | `d2c16bd7c7fb2344408259fd083c8fdbeda3b096b8c9d07e48848e99fc2c3743` |

In the original ELF, `VideoSend` is at virtual address `049f0128`, V1 serialization at `049f5c74`, and V2 serialization at `049f6448`. `UDjiDFSMGameInstance::OnBackBufferReady_RenderThread` at `04f64a9c` selects 1920x1080 for ZV900. Its compression callback is `OnFinishCompress` at `04f663a0`. The Android encoder uses H.264, 60 fps, 10,485,760 bits/s, and a three-second I-frame interval.

The mobile app sends V2 packets with up to 65,404 encoded bytes per fragment and optional LogicLink wrapping. This format matched native output in emulation but displayed a black screen through the PC USB interface. The bridge's V1 adaptation is described below. Full mobile layouts are in the [mobile protocol](research/MOBILE_PROTOCOL.md).

## USB and session

The device enumerates with VID `2ca3` and PID `0020`. The video and control interface is **MI04**, with class/subclass/protocol `ff/43/01`, bulk OUT `04`, bulk IN `85`, and a 512-byte endpoint maximum packet size. The backend requires this descriptor and rejects ambiguous devices. It leaves the USB configuration unchanged.

DJI's x64 libusb0 application binary interface (ABI) adds a 512-byte location field to the device structure. The upstream PyUSB structure is incompatible, so the backend checks for the DLL hash above and PyUSB 1.3.1. It uses 16 KiB writes by default. The optional 2 KiB mode also displayed video correctly. Consecutive V1 packets can share a write, and both sizes produce identical reassembled bytes. The DLL splits requests larger than 65,536 bytes internally.

On macOS, libusb-1.0 claims MI04 directly and needs no driver. macOS binds a driver only to MI02, the mass-storage interface. In Virtual Flight mode, a USB-C-to-USB-C cable lets the goggles take the host role and switches the Mac's port to device mode, so the Mac needs a USB-A-to-USB-C cable.

The macOS libusb backend clears the endpoint halt after every read that times out, and the bridge's 50 ms reads time out between replies. A 120-second stream still kept its heartbeat.

PC control uses V1 source type 10, ID 1. Each reply must match the route, command, and sequence and contain a success status. The bridge sends these requests before video:

| Request | Destination | Payload |
| --- | --- | --- |
| Heartbeat `00/00` | Type 28, ID 0 | `000020000000` |
| Identity `00/ff` | Type 28, ID 1 | `00` |
| Simulator mode `0b/1a` | Type 28, ID 0 | `01010000` |

The tested identity contains `E3T ZV900 AC`. Heartbeats continue every 500 ms. A successful simulator-mode reply completes the handshake. Outside the Virtual Flight screen, heartbeat and identity are still answered, but simulator mode gets no reply from `28/0` and status `e0` from `28/1`, `28/2`, and `28/4`. The bridge reports this case as Virtual Flight not being open and keeps retrying. Video acceptance was checked separately with a moving test pattern.

## Video packets

Video uses command `0b/53` in raw DUML V1, with no acknowledgement requested, from source type 10, ID 1 to destination type 28, ID 1. The 1,023-byte V1 limit leaves **978 encoded bytes** after 13 bytes of framing and checksums and 32 bytes of metadata. Control and video share a 16-bit sequence allocator that skips outstanding control requests.

The video payload starts with this little-endian structure, followed by the encoded chunk:

| Offset | Bytes | Field |
| --- | ---: | --- |
| 0 | 1 | Zero |
| 1 | 1 | Frame needs multiple fragments |
| 2 | 1 | Last fragment of frame |
| 3 | 1 | Zero |
| 4 | 4 | Global fragment counter, wrapping at 32 bits |
| 8 | 8 | Millisecond timestamp, truncated as described below |
| 16 | 4 | Metadata size: 32 |
| 20 | 4 | Encoded bytes in this chunk |
| 24 | 1 | First fragment of frame |
| 25 | 7 | Zero padding |

The receiver accepts at most about 65,536 bytes per frame; see the [frame-size measurements](research/EXPERIMENTS.md#frame-size-limit-and-bitrate). Frames of 42 KB and below displayed. Streams whose keyframes reached 67 KB and above appeared late, froze, or stayed black, and a frozen receiver only recovered after a power cycle. This matches the 65,404-byte fragment limit of the mobile protocol. The bridge never sends a larger frame.

The timestamp follows the native operation: sample Unix milliseconds, retain the low 32 bits, interpret them as signed, then sign-extend to 64 bits. The packet header CRC8 uses seed `0x77` and reflected polynomial `0x8c`. CRC16 uses seed `0x3692` and reflected polynomial `0x8408`, with no final XOR.

By default, the reader uses nonseekable, single-stream AVI to identify packet lengths and release complete H.264 packets immediately. AVI headers stay on the PC. This mode also removes filler network abstraction layer (NAL) units, type 12. Sequence and picture parameter sets (SPS/PPS) and picture NAL units remain unchanged. With `--no-low-latency`, the reader instead splits H.264 pictures at Annex-B access unit delimiters (AUDs), then removes each AUD. Both readers require the configured encoder output.

## Capture and encoding

The Windows Graphics Capture path uses GPU bicubic scaling, preserves the source aspect ratio, and passes D3D11 textures directly to NVENC. The tested preset targets 1080p at 100 fps and 20 Mbps, with preset `p4`, ultra-low-latency tuning, no B frames, a two-frame rate-control buffer, and three-second keyframes.

Desktop Duplication at a requested 100 fps produced repeated frames in the initial static-desktop test. A moving source and Windows Graphics Capture improved observed smoothness. The distinct frame rate at the goggles and end-to-end latency have not been measured.

On macOS 12.3 and later, a Swift helper, `goggles_bridge/mac_capture.swift`, replaces FFmpeg. ScreenCaptureKit delivers NV12 frames that are already scaled and centered in 1920x1080, and VideoToolbox encodes each one as it arrives. The encoder uses low-latency rate control with no frame reordering. Frames go out as they're captured, up to the `--capture-fps` ceiling, so a 120 Hz display can produce more than 100 fps. When the screen is still, ScreenCaptureKit reports idle frames, and the helper encodes the last picture again so the goggles keep receiving video. The helper writes the same AVI or Annex-B framing as FFmpeg, AUDs included, so the bridge reads both the same way. The bridge compiles the helper with `xcrun swiftc` on first use and names the binary after the source hash, so an edited source is rebuilt.

On older macOS, or with `--capture avfoundation`, FFmpeg's AVFoundation input captures the screen in NV12, and `scale_vt` fits it to 1920x1080 on the GPU. CPU padding centers the result. The `h264_videotoolbox` encoder runs with `-flags +low_delay`, which selects low-latency rate control. In that mode the stream signals no frame reordering, and every picture is a long-term reference. VideoToolbox writes no AUDs, so the `h264_metadata` bitstream filter adds them for the frame readers. Low-latency mode rejects plain Baseline, so `--h264-profile baseline` selects Constrained Baseline on macOS.

A slight brightness pulse followed the keyframe interval when it changed from one second to three seconds. An offline intra-refresh trial reduced the synthetic keyframe dip but increased scene-change bursts, so it was not adopted. Constant-quality encoding remains untested.

See the [research index](research/README.md) for native addresses, packet examples, experiment results, and reproduction scripts. The analysis covers application binaries and USB traffic. The goggles firmware was neither decoded nor modified, and other device models and firmware versions remain untested.
