<p align="center">
  <img src="./assets/logo.png" width="500" alt="POV Printer Logo">
</p>

<h1 align="center">POV PRINTER</h1>
<p align="center">
  A DIY Persistence-of-Vision Display That Prints Images In Thin Air
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Built%20With-Python%20%2B%20Arduino-blue?style=for-the-badge">
  <img src="https://img.shields.io/badge/Status-Work%20In%20Progress-orange?style=for-the-badge">
  <img src="https://img.shields.io/badge/Made%20In-Abuja%2C%20NG-green?style=for-the-badge">
</p>

### 🎥 Demo
> Add a GIF or YouTube link here. This is the most important part!
> `https://youtube.com/your-video-link`

![Demo GIF](./assets/demo.gif)

### How It Works
A strip of LEDs spinning at high speed. We flash each LED at precise microsecond timings to draw an image in the air using persistence of vision. This repo contains the Python image processor + Arduino firmware.

1.  Python script converts any image -> 1D pixel columns
2.  Sends data to Arduino via Serial
3.  Arduino syncs with motor rotation (hall sensor) and flashes LEDs

### Hardware Needed
- Arduino Uno / Nano
- WS2812B LED Strip
- DC Motor + Hall Sensor (A3144)
- 12V Power Supply
- 3D Printed / Wood frame

### Software Setup

**1. Clone the repo**
```bash
git clone https://github.com/YOUR_USERNAME/pov-printer.git
cd pov-printer
