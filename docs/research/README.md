# Reverse-engineering data and tools

Windows and macOS video input work through the stock DJI Goggles 2 PC USB interface in Virtual Flight mode. The tested device identified itself as ZV900 and had reported firmware 01.11.00.00. The same device on firmware 01.14.00.00 also displayed the test pattern and the Windows desktop; see [firmware 01.14](EXPERIMENTS.md#firmware-0114). A moving test pattern, the Windows desktop, Uncrashed, and the macOS desktop displayed in the lenses.

The bridge carries the mobile application's 32-byte video metadata in **DUML V1 `0b/53` packets on MI04**, with 978 encoded bytes per fragment. The original mobile V2 envelope matched native output in emulation but did not display video through this PC connection.

## Protocol and analysis

The following pages describe the protocol and how to reproduce the analysis:

| Page | Contents |
| --- | --- |
| [PC protocol](../PROTOCOL.md) | USB interface, handshake, and V1 video packets |
| [Mobile protocol](MOBILE_PROTOCOL.md) | Android call chain, V1/V2 layouts, LogicLink, and timestamps |
| [Artifacts](ARTIFACTS.md) | Download sources, versions, hashes, and analysis tools |
| [Native analysis](NATIVE_ANALYSIS.md) | ARM addresses, Windows relative virtual addresses, and USB driver functions |
| [Experiments](EXPERIMENTS.md) | Transport tests, host timings, and encoder quality measurements |
| [Reproduction](REPRODUCE.md) | Emulation, Ghidra scripts, packet fixtures, and benchmarks |
| [Stereo and head tracking](STEREO.md) | Separate views for each eye, head orientation over USB, and SteamVR |
| [Goggles 3 probe](GOGGLES3.md) | Reported Goggles 3 USB details, a step-by-step hardware probe, and Android OTG role tests |
| [Open questions](OPEN_QUESTIONS.md) | Receiver timing, frame pacing, quality, and USB interruptions |
| [Related work](RELATED.md) | External sources, device models, and video input or output support |

## Evidence files

The repository includes these records:

- [REA inventory](evidence/rea-inventory.json): three records, 180 archive occurrences, 152 graph nodes, and 179 edges, with hashes and original evidence identifiers. Six local APK path fields use `${WORK}`. No inventory records were removed.
- [APK ZIP inventory](evidence/apk-zip-inventory.json) and [Windows archive provenance](evidence/windows-archive-provenance.json).
- [PC control exchanges](evidence/pc-control-session.json): request and reply bytes, original log line numbers, and the log hash. All included packets pass the V1 CRC checks.
- [Native emulation fixtures](../../tests/fixtures) and their [hash manifest](evidence/fixture-manifest.json).
- [Live host telemetry](evidence/live-host-telemetry.json): per-interval statistics and connection events from the November 7 comparisons.
- `quality-*.json` in the [evidence directory](evidence): per-frame structural similarity (SSIM), peak signal-to-noise ratio (PSNR), and encoded sizes. Desktop reference images are not included.
- [CRC benchmark](evidence/crc-benchmark.json), [USB write equivalence](evidence/usb-write-equivalence.json), and [timing-metadata removal results](evidence/timing-sei-offline.json).

The [analysis tools](../../tools/research) include ARM emulation harnesses and Ghidra scripts. They require separately acquired vendor binaries. APKs, executables, firmware, decompiled vendor code, personal desktop recordings, device serials, and signed download URLs are excluded.

The analysis covers Virtual Flight applications, native libraries, and PC USB traffic. Goggles firmware and decoder code were not recovered. Panel frame rate and screen-to-lens latency remain unmeasured.
