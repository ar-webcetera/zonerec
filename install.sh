#!/usr/bin/env bash
# Установка ZoneRec: системные зависимости, бинарь в ~/.local/bin, автозапуск.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

echo ">> Системные зависимости (нужен sudo)"
sudo apt update
sudo apt install -y ffmpeg python3-gi gir1.2-gtk-3.0 \
    gir1.2-keybinder-3.0 gir1.2-notify-0.7 libnotify-bin xdg-utils pulseaudio-utils
# Индикатор в трее: один из двух пакетов (в зависимости от дистрибутива)
sudo apt install -y gir1.2-appindicator3-0.1 \
  || sudo apt install -y gir1.2-ayatanaappindicator3-0.1 \
  || echo "!! AppIndicator не установлен — приложение будет работать по хоткеям без иконки в трее"

echo ">> Установка бинаря"
install -Dm755 "$HERE/zonerec.py" "$HOME/.local/bin/zonerec"
install -Dm644 "$HERE/assets/zonerec.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/zonerec.svg"
install -Dm644 "$HERE/assets/zonerec.svg" "$HOME/.local/share/pixmaps/zonerec.svg"

echo ">> Автозапуск + ярлык"
tmp_desktop="$(mktemp)"
sed "s|^Exec=.*|Exec=$HOME/.local/bin/zonerec|" "$HERE/zonerec.desktop" > "$tmp_desktop"
install -Dm644 "$tmp_desktop" "$HOME/.config/autostart/zonerec.desktop"
install -Dm644 "$tmp_desktop" "$HOME/.local/share/applications/zonerec.desktop"
rm -f "$tmp_desktop"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" || true
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "!! Добавьте ~/.local/bin в PATH (обычно уже есть после релогина)";;
esac

echo
echo "Готово. Запуск сейчас:  zonerec  (или из меню приложений «ZoneRec»)."
echo "После перелогина стартует автоматически и висит в трее."
echo "Хоткеи по умолчанию: Super+Shift+R — выбрать зону и запись, Super+Shift+S — стоп."
echo "Настройки: ~/.config/zonerec/config.ini"
