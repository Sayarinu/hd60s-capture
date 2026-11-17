pkgname=hd60s-capture
pkgver=0.1.0
pkgrel=1
pkgdesc='GTK capture and preview app for the Elgato Game Capture HD60 S'
arch=('any')
url='https://github.com/Sayarinu/hd60s-capture'
license=('MIT')
depends=('python' 'python-gobject' 'gtk4' 'libadwaita>=1.6' 'libgudev'
         'gstreamer' 'gst-plugins-base-libs' 'gst-plugins-good'
         'gst-plugin-pipewire' 'pipewire-pulse' 'libpulse' 'v4l-utils')
makedepends=('desktop-file-utils')
optdepends=('gst-plugin-gtk4: accelerated video preview'
            'gst-plugins-bad: NVIDIA NVENC H.264 recording'
            'gst-libav: AAC audio in recordings')

check() {
    cd "$startdir"
    python -m compileall -q elgato_capture
    desktop-file-validate packaging/studio.nouvellelune.ElgatoCapture.desktop
}

package() {
    cd "$startdir"
    install -Dm644 -t "$pkgdir/usr/lib/$pkgname/elgato_capture" elgato_capture/*.py
    install -Dm755 packaging/hd60s-capture "$pkgdir/usr/bin/$pkgname"
    install -Dm644 -t "$pkgdir/usr/share/applications" packaging/*.desktop
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
