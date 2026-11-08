# Goggles 2 diagnostic reads

These requests were tested over USB MI04 on Goggles 2 firmware 01.11.00.00. The manifest read also works on 01.14.00.00. All addresses below use DUML device type and ID. Packet framing and checksums use the existing [V1 serializer](../../goggles_bridge/control.py).

## Read the firmware manifest

Send command `00/4f` from `10/1` to `28/5`, with acknowledgment type 2. The request payload contains these fields:

| Offset | Size | Value |
| --- | --- | --- |
| 0 | 1 | `01` |
| 1 | 4 | Byte offset, little-endian |
| 5 | 4 | Requested length, little-endian; tested with 1000 |

The successful reply starts with status `00`, then two little-endian 32-bit fields: returned byte count and remaining byte count. Data follows at offset 9. The device returned at most 256 bytes per request. Advance the requested offset by the returned count.

The complete response was a 3,200-byte `IM*H` container. Its SHA-256 is `ae68fccc9300febf2588764200b85af5a0ba49102421c67e235f7990df894142`.

The container has a 224-byte header, a 384-byte signature field, and 2,592 payload bytes. Its stored payload SHA-256 matched. RSA signature verification was not performed. The unencrypted XML identifies device `zv900` and firmware `01.11.0000`:

| Module ID | Name | Version | V1 address |
| --- | --- | --- | --- |
| `2803` | `CPLD` | `34.08.30.02` | `28/3` |
| `2805` | `ZV900_E3T` | `10.00.81.54` | `28/1` |

The manifest names the E3T image `zv900_2805_v10.00.81.54_20241224.pro.fw.sig`. Reading this manifest does not download that image.

On firmware 01.14.00.00 the container was again 3,200 bytes, returned in 13 replies, with SHA-256 `85747ef29795ff4bc8017003d7c3ca57fb810252036a7284b0312d146b7904af`. Its XML names firmware `01.14.0000`, released October 19, 2026. CPLD is unchanged at `34.08.30.02`. `ZV900_E3T` is `10.00.81.97`, image `zv900_2805_v10.00.81.97_20260811.pro.fw.sig`.

[`goggles_bridge/firmware.py`](../../goggles_bridge/firmware.py) implements this read, and the [goggles probe](../../tools/research/probe_goggles.py) runs it after identity unless `--no-firmware` is given.

## Read the diagnostic index

The log service uses address `29/2`, named `G-E3T` in its index. Send from PC address `10/0`:

1. Send command `00/ea`, acknowledgment type 2, payload `0a 01 01`.
2. Read its success reply. This device returned the path `/blackbox/info/export_list.json`.
3. Send command `00/2a`, acknowledgment type 2, with the following payload:

   ```text
   08 80 | path plus NUL, padded to 128 bytes | 09 01 00
   ```

Without step 1, the index request returned `df`. After preparation, it returned `00` and a transfer-start message.

The transfer arrives inside wrappers beginning `21 12 ad de`. Embedded V1 packets use command `00/2a`. A start payload begins with `01`, a 32-bit file length, a filename length byte, and the filename. Reply to that start packet with reversed addresses, the same sequence, response flag 1, acknowledgment type 0, and payload `00 d0 03 a0 0f 01 01`.

Data payloads begin with `04` and a 32-bit part number. The tested acknowledgment selects 976 file bytes per part. Each captured data payload also had 12 trailing bytes outside the file data. Use the announced file length to trim the final part. Check packet CRCs, require contiguous part numbers, and reject changed retransmissions.

This transfer yielded 76,392 bytes of valid JSON in 79 parts. The index contained 281 log entries and 73 boot entries. Its module names were `flyctrl`, `gls_flyctrl`, `info`, `sdrs`, and `system`. It contained no FTP credentials.

Progress and completion acknowledgments were sent using the captured Assistant sequence. Reliable transfer completion and recovery need further testing before this becomes a general downloader.

## FTP and file access

DJI Assistant's `DJIGlsService.dll` reads `ftp_user` and `ftp_pwd` from JSON in function `0x1010b390`. The DLL's SHA-256 is `2dff0ad378e623d9ec5c8f1b6508b3750a0cd40628a9db571301b3904f80a808`. Ghidra 12.1.4 located the `ftp_user` reference at `0x1010c907`. No accepted FTP login was recovered.

A file-read request for `/blackbox/info/` returned `e3`. A request for `/system/bin/dji_glasses` returned `d6`. Those results apply to the tested requests and session state; they do not establish whether another file-read operation is available.

No head-orientation query or separate-eye display command was found. The diagnostic index provides log locations, not live sensor values.

## Sources

The first config request and export sequence came from [Goggles 3 USB captures](https://github.com/ggurov/dji-goggles3-display-unit/blob/master/goggles_tool/goggles_tool/protocol/templates.py). The responses described here were then observed on Goggles 2. The [DUML dissector](https://github.com/o-gs/dji-firmware-tools/blob/master/comm_dissector/wireshark/dji-dumlv1-general.lua) names `00/4f` as Get Cfg File. The [IM*H parser](https://github.com/o-gs/dji-firmware-tools/blob/master/dji_imah_fwsig.py) defines the container fields.
