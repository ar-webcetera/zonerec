#!/usr/bin/env bash
# Build a Debian/Ubuntu package for ZoneRec.
set -euo pipefail

APP=zonerec
VERSION="${VERSION:-0.1.12}"
ARCH=all
ROOT="build/${APP}_${VERSION}_${ARCH}"
DEB="dist/${APP}_${VERSION}_${ARCH}.deb"

rm -rf "$ROOT"
mkdir -p \
  "$ROOT/DEBIAN" \
  "$ROOT/usr/bin" \
  "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$ROOT/usr/share/pixmaps" \
  "$ROOT/etc/xdg/autostart" \
  "$ROOT/usr/share/doc/$APP"

install -m 0755 zonerec.py "$ROOT/usr/bin/zonerec"
install -m 0644 assets/zonerec.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/zonerec.svg"
install -m 0644 assets/zonerec.svg "$ROOT/usr/share/pixmaps/zonerec.svg"
install -m 0644 zonerec.desktop "$ROOT/usr/share/applications/zonerec.desktop"
install -m 0644 zonerec.desktop "$ROOT/etc/xdg/autostart/zonerec.desktop"
sed -i 's|^Exec=.*|Exec=/usr/bin/zonerec|' \
  "$ROOT/usr/share/applications/zonerec.desktop" \
  "$ROOT/etc/xdg/autostart/zonerec.desktop"
install -m 0644 README.md LICENSE "$ROOT/usr/share/doc/$APP/"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $APP
Version: $VERSION
Section: video
Priority: optional
Architecture: $ARCH
Maintainer: Andrey Romanov
Depends: ffmpeg, python3, python3-gi, gir1.2-gtk-3.0, gir1.2-gdkpixbuf-2.0, gir1.2-keybinder-3.0, gir1.2-notify-0.7, libnotify-bin, xdg-utils, pulseaudio-utils, gir1.2-appindicator3-0.1 | gir1.2-ayatanaappindicator3-0.1
Description: Record a selected X11 screen area
 ZoneRec is a small tray utility for Linux/X11 that lets you select a screen
 area and record it to an MP4 file or screenshot using ffmpeg.
EOF

cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/usr/bin/env bash
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
EOF
chmod 0755 "$ROOT/DEBIAN/postinst"

cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/usr/bin/env bash
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
EOF
chmod 0755 "$ROOT/DEBIAN/postrm"

mkdir -p dist
dpkg-deb --build --root-owner-group "$ROOT" "$DEB"

echo "Built $DEB"
echo "Install: sudo apt install ./$DEB"
