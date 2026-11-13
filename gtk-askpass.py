#!/usr/bin/python3
"""Standalone GTK askpass helper for SSH and remote sudo."""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def main():
    dialog = Gtk.Dialog(title="Authentication")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    label = Gtk.Label(label=sys.argv[1] if len(sys.argv) > 1 else "Password:")
    label.set_line_wrap(True)
    box.add(label)
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_activates_default(True)
    box.add(entry)
    dialog.show_all()
    response = dialog.run()
    if response == Gtk.ResponseType.OK:
        print(entry.get_text(), flush=True)
    dialog.destroy()
    return 0 if response == Gtk.ResponseType.OK else 1


if __name__ == "__main__":
    sys.exit(main())
