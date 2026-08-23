# qBittorrent - FitGirl Repacks Search Plugin

*Read this in other languages: [English](README.md), [Español](README.es.md)*

Search plugin for the qBittorrent BitTorrent client to query and index game releases from fitgirl-repacks.site.

## Features

- Multi-threaded concurrent requests for optimized response times.
- Direct extraction of magnet URIs and torrent files.
- Accurate detection of repack download sizes and publication timestamps.
- Automatic filtering of non-game digest and administrative posts.

## Installation

### Method 1: Direct Installation via URL (Recommended)

1. Open **qBittorrent** and navigate to the **Search** tab.
2. Click the **Search plugins...** button in the lower right corner.
3. Click **Install a new one**.
4. Select **Web link** and paste the following URL:
   ```text
   https://raw.githubusercontent.com/afalvarezsite/qbtFitGirl-search/main/fitgirl_repacks.py
   ```
5. Click **OK** to complete the installation.

### Method 2: Manual Installation

1. Download the `fitgirl_repacks.py` file from this repository.
2. In qBittorrent, go to **Search** > **Search plugins...** > **Install a new one**.
3. Select **Local file** and choose the downloaded `fitgirl_repacks.py` file.

## Requirements

- qBittorrent v4.x or later.
- Python 3 runtime environment configured on the system.

## License

This project is licensed under the terms of the GNU General Public License v3.0 (GPLv3). See the [LICENSE](LICENSE) file for details.

## Disclaimer

This software is provided for informational and technical interoperability purposes only. The project does not host any copyrighted material nor is it affiliated with the indexed website.
