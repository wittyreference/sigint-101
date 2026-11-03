# ABOUTME: Tests for deploy/desktop/apply-theme.sh (the phosphor-terminal desktop) run as real bash.
# ABOUTME: gsettings, fc-cache, fc-list, pgrep and friends are stubs on PATH (the OS boundary).

from pathlib import Path

import pytest

from tests.test_kiosk_deploy import INSTALL, Box

REPO = Path(__file__).resolve().parent.parent
THEME = REPO / "deploy" / "desktop" / "apply-theme.sh"


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()


def test_a_box_without_fontconfig_still_installs_the_theme(box):
    # A box with no fontconfig yet (a bare image provisioned over SSH) gets every file; the font
    # cache and the font choice wait for the desktop's own fontconfig.
    (box.bin / "fc-cache").unlink()
    (box.bin / "fc-list").unlink()
    run = box.run(THEME)
    assert run.returncode == 0, run.stderr
    assert (box.home / ".themes/SIGINT101/gtk-3.0/gtk.css").is_file()
    assert "gsettings set org.gnome.desktop.interface font-name PT Mono 11" not in box.calls(
        "gsettings")


def _keys(path):
    return [ln for ln in path.read_text().splitlines() if "=" in ln]


def test_the_theme_installs_the_gtk_theme_frames_wallpaper_and_fonts(box):
    run = box.run(THEME)
    assert run.returncode == 0, run.stderr
    home = box.home
    assert (home / ".themes/SIGINT101/gtk-3.0/gtk.css").is_file()
    assert (home / ".themes/SIGINT101/index.theme").is_file()
    assert (home / ".config/labwc/themerc-override").is_file()
    assert (home / ".local/share/sigint101/wallpaper.png").is_file()
    assert list((home / ".local/share/fonts/sigint101").glob("ptmono-*.woff2"))
    assert list((home / ".local/share/fonts/sigint101").glob("russoone-*.woff2"))


def test_the_theme_selects_the_gtk_theme_and_the_mono_font(box):
    box.run(THEME)
    calls = box.calls("gsettings")
    assert "gsettings set org.gnome.desktop.interface gtk-theme SIGINT101" in calls
    assert "gsettings set org.gnome.desktop.interface font-name PT Mono 11" in calls


def test_every_pcmanfm_screen_file_gets_the_wallpaper_and_green_labels(box):
    # On the Wayland desktop pcmanfm keeps one file per screen; the old wallpaper is replaced.
    desk = box.home / ".config/pcmanfm/LXDE-pi"
    desk.mkdir(parents=True)
    screen = desk / "desktop-items-HDMI-A-1.conf"
    screen.write_text("[*]\nwallpaper=/usr/share/rpd-wallpaper/fisherman.jpg\nshow_trash=1\n")
    box.run(THEME)
    keys = _keys(screen)
    assert f"wallpaper={box.home}/.local/share/sigint101/wallpaper.png" in keys
    assert "desktop_bg=#000000000000" in keys
    assert "desktop_fg=#2e2ee6e66a6a" in keys
    assert "show_trash=1" in keys                      # the learner's other settings stay
    assert not (desk / "desktop-items-0.conf").exists()


def test_a_desktop_file_is_made_when_pcmanfm_has_none_yet(box):
    box.run(THEME)
    made = box.home / ".config/pcmanfm/LXDE-pi/desktop-items-0.conf"
    assert "desktop_bg=#000000000000" in _keys(made)


def test_applying_twice_sets_each_key_once(box):
    box.run(THEME)
    box.run(THEME)
    desk = _keys(box.home / ".config/pcmanfm/LXDE-pi/desktop-items-0.conf")
    term = _keys(box.home / ".config/lxterminal/lxterminal.conf")
    assert len(desk) == len({k.split("=")[0] for k in desk})
    assert len(term) == len({k.split("=")[0] for k in term})


def test_the_terminal_goes_phosphor_green_on_black_in_pt_mono(box):
    box.run(THEME)
    term = _keys(box.home / ".config/lxterminal/lxterminal.conf")
    assert "fgcolor=rgb(46,230,106)" in term
    assert "bgcolor=rgb(0,0,0)" in term
    assert "fontname=PT Mono 11" in term


def test_install_applies_the_desktop_theme(box):
    run = box.run(INSTALL)
    assert run.returncode == 0, run.stderr
    assert "desktop theme: SIGINT101 applied" in run.stdout
    assert (box.home / ".themes/SIGINT101/gtk-3.0/gtk.css").is_file()
