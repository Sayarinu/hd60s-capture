# HD60 S Capture

An unofficial Linux desktop app for previewing, recording, and taking screenshots from the Elgato Game Capture HD60 S USB capture card. It supports fullscreen preview, HDMI audio monitoring, and V4L2 picture controls.

## Install on Arch Linux

Install the [HD60 S Linux driver](https://github.com/dougg3/hd60s-linux-driver#how-to-build-and-use) separately. Plug the card into USB 3.0 and connect an HDMI source. The app discovers the card through V4L2 and uses PipeWire for audio.

Download `hd60s-capture-*-any.pkg.tar.zst` and `SHA256SUMS` from the [latest release](https://github.com/Sayarinu/hd60s-capture/releases/latest). From the download directory, run the following commands with one version of the package present.

```sh
sha256sum -c SHA256SUMS
sudo pacman -U ./hd60s-capture-*-any.pkg.tar.zst
```

Open **HD60 S Capture** from your app menu or run `hd60s-capture`. Press **F12** for a PNG screenshot or **F9** to start or stop an MP4 recording. By default, screenshots go to `Pictures/Elgato` and recordings to `Videos/Elgato` in your home directory, respecting your XDG folder settings.

Recording requires an NVIDIA GPU with NVENC, a working NVIDIA driver, and `gst-plugins-bad`. When the card exposes an audio input, recording also requires `gst-libav` for AAC encoding. Preview and screenshots work independently of NVENC. The optional `gst-plugin-gtk4` package enables the accelerated preview path.

## Build and release

On Arch Linux, install `base-devel` and run `makepkg -s` from the checkout to build a package. With the runtime dependencies installed, `python -m elgato_capture` runs the app directly from the repository.

For a new release, update `pkgver` in `PKGBUILD`, commit the change, and push a matching tag such as `v0.1.1`. GitHub Actions builds the `.pkg.tar.zst` and publishes it with its SHA-256 checksum on GitHub Releases. Download and install the new package with `pacman -U` to upgrade.

## License

The app is licensed under the [MIT License](LICENSE). The separately installed kernel driver has its own license. Elgato and Game Capture identify the supported hardware. This is an independent project.
