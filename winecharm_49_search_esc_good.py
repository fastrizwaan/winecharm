#!/usr/bin/env python3

import gi
import threading
import subprocess
import os
import shutil
import shlex
import hashlib
import signal
import re
import yaml
from pathlib import Path
import sys
import socket
import time
import glob
import fnmatch
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import GLib, Gio, Gtk, Gdk, Adw, GdkPixbuf, Pango

author = "Mohammed Asif Ali Rizvan"
email = "fast.rizwaan@gmail.com"
copyright = "GNU General Public License (GPLv3+)"
website = "https://github.com/fastrizwaan/WineCharm"
appname = "WineCharm"
version = "0.6"

# These need to be dynamically updated:
runner = ""  # which wine
wine_version = ""  # runner --version
template = ""  # default: WineCharm-win64 ; #if not found in settings.yaml at winecharm directory add default_template
arch = ""  # default: win64 ; # #if not found in settings.yaml at winecharm directory add win64

winecharmdir = Path(os.path.expanduser("~/.var/app/io.github.fastrizwaan.WineCharm/data/winecharm")).resolve()
prefixes_dir = winecharmdir / "Prefixes"
templates_dir = winecharmdir / "Templates"
default_template = templates_dir / "WineCharm-win64"

applicationsdir = Path(os.path.expanduser("~/.local/share/applications")).resolve()
tempdir = Path(os.path.expanduser("~/.var/app/io.github.fastrizwaan.WineCharm/data/tmp")).resolve()
iconsdir = Path(os.path.expanduser("~/.local/share/icons")).resolve()

SOCKET_FILE = winecharmdir / "winecharm_socket"

class WineCharmApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.fastrizwaan.WineCharm')
        Adw.init()
        self.connect("activate", self.on_activate)
        self.connect("startup", self.on_startup)
        self.wine_process_running = False
        self.selected_script = None
        self.selected_script_name = None
        self.selected_row = None
        self.spinner = None
        self.new_scripts = set()
        self.initializing_template = False
        self.running_processes = {}
        self.play_stop_handlers = {}
        self.options_listbox = None
        self.flowbox_state = None
        
        self.search_active = False
        self.command_line_file = None

        # Register the SIGINT signal handler
        signal.signal(signal.SIGINT, self.handle_sigint)

        self.hamburger_actions = [
            ("🛠️ Settings", self.on_settings_clicked),
            ("☠️ Kill all", self.on_kill_all_clicked),
            ("📂 Import Wine Directory", self.on_import_wine_directory_clicked),
            ("❓ Help", self.on_help_clicked),
            ("📖 About", self.on_about_clicked),
            ("🚪 Quit", self.quit_app)
        ]

        self.css_provider = Gtk.CssProvider()
        self.css_provider.load_from_data(b"""
           .menu-button.flat:hover {
                background-color: @headerbar_bg_color;
            }
            .button-box button {
                min-width: 80px;
                min-height: 30px;
            }
            .options-listbox .listbox-row:hover,
            .full_listbox .listbox-row:hover,
            .listbox .listbox-row:hover {
                background-color: #f0f0f0;
            }
            .options-listbox .listbox-row,
            .full_listbox .listbox-row,
            .listbox .listbox-row {
                padding: 10px;
            }
            .options-listbox .listbox-row label,
            .full_listbox .listbox-row label,
            .listbox .listbox-row label {
                font-weight: bold;
                font-size: 14px;
            }
            .listbox-row {
                min-height: 40px;
            }
            .common-background {
                background-color: @theme_base_color;
            }
            .new-script-button {
                background-color: lightblue;
            }
            .normal-font {  /* Add the CSS rule for the normal-font class */
            font-weight: normal;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.connect("clicked", self.on_back_button_clicked)
        self.open_button_handler_id = None

    def on_startup(self, app):
        self.create_main_window()
        GLib.idle_add(self.create_script_list)

        missing_programs = self.check_required_programs()
        if missing_programs:
            self.show_missing_programs_dialog(missing_programs)
        else:
            if not default_template.exists():
                self.initialize_template(default_template, self.on_template_initialized)
            else:
                # Template already exists, process the CLI file if provided
                self.set_dynamic_variables()
                if self.command_line_file:
                    self.process_cli_file(self.command_line_file)

        self.generate_about_yml()
        self.generate_settings_yml()

        # Run the check_running_processes_and_update_buttons asynchronously
        self.check_running_processes_and_update_buttons()

        # Start the process monitor in a separate thread
        threading.Thread(target=self.monitor_processes, daemon=True).start()

    def on_activate(self, app):
        self.window.present()

    def on_shutdown(self, app):
        # Perform any necessary cleanup actions here
        if SOCKET_FILE.exists():
            SOCKET_FILE.unlink()

    def handle_sigint(self, signum, frame):
        print("Received SIGINT. Cleaning up and exiting.")
        self.quit()

    def quit_app(self, action=None, param=None):
        self.quit()

    def start_socket_server(self):
        def server_thread():
            if SOCKET_FILE.exists():
                SOCKET_FILE.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(SOCKET_FILE))
                server.listen()
                while True:
                    conn, _ = server.accept()
                    with conn:
                        message = conn.recv(1024).decode()
                        if message:
                            # Extract the current working directory and file path
                            cwd, file_path = message.split("||")
                            base_path = Path(cwd)
                            pattern = file_path.split("/")[-1]
                            directory = "/".join(file_path.split("/")[:-1])
                            search_path = base_path / directory

                            if not search_path.exists():
                                print(f"Directory does not exist: {search_path}")
                                continue

                            abs_file_paths = list(search_path.glob(pattern))
                            if abs_file_paths:
                                for abs_file_path in abs_file_paths:
                                    if abs_file_path.exists():
                                        print(f"Resolved absolute file path: {abs_file_path}")  # Debugging output
                                        GLib.idle_add(self.process_cli_file, str(abs_file_path))
                                    else:
                                        print(f"File does not exist: {abs_file_path}")
                            else:
                                print(f"No files matched the pattern: {file_path}")

        threading.Thread(target=server_thread, daemon=True).start()

    def process_cli_file_when_ready(self):
        while self.initializing_template:
            time.sleep(1)
        GLib.idle_add(self.process_cli_file, self.command_line_file)

    def process_cli_file(self, file_path):
        self.show_processing_spinner("Processing")
        threading.Thread(target=self._process_cli_file, args=(file_path,)).start()

    def _process_cli_file(self, file_path):
        print(f"Processing CLI file: {file_path}")
        abs_file_path = str(Path(file_path).resolve())
        print(f"Resolved absolute CLI file path: {abs_file_path}")

        try:
            if not Path(abs_file_path).exists():
                print(f"File does not exist: {abs_file_path}")
                return
            self.create_yaml_file(abs_file_path, None)
            GLib.idle_add(self.create_script_list)
        except Exception as e:
            print(f"Error processing file: {e}")
        finally:
            GLib.idle_add(self.hide_processing_spinner)

    def set_dynamic_variables(self):
        global runner, wine_version, template, arch
        runner = subprocess.getoutput('which wine')
        wine_version = subprocess.getoutput(f"{runner} --version")
        template = "WineCharm-win64" if not (winecharmdir / "settings.yml").exists() else self.load_settings().get('template', "WineCharm-win64")
        arch = "win64" if not (winecharmdir / "settings.yml").exists() else self.load_settings().get('arch', "win64")

    def load_settings(self):
        settings_file_path = winecharmdir / "settings.yml"
        if settings_file_path.exists():
            with open(settings_file_path, 'r') as settings_file:
                return yaml.safe_load(settings_file)
        return {}

    def generate_about_yml(self):
        about_file_path = winecharmdir / "About.yml"
        if about_file_path.exists():
            print(f"{about_file_path} already exists. Skipping generation.")
            return

        about_data = {
            "Application": appname,
            "Version": version,
            "Copyright": copyright,
            "Website": website,
            "Author": author,
            "E-mail": email,
            "Wine_Runner": runner,
            "Wine_Version": wine_version,
            "Template": template,
            "Wine_Arch": arch,
            "WineZGUI_Prefix": str(winecharmdir),
            "Wine_Prefix": str(default_template),
            "Creation_Date": time.strftime("%a %b %d %I:%M:%S %p %Z %Y")
        }

        with open(about_file_path, 'w') as about_file:
            yaml.dump(about_data, about_file, default_flow_style=False)
        print(f"Generated {about_file_path}")

    def generate_settings_yml(self):
        settings_file_path = winecharmdir / "settings.yml"
        if settings_file_path.exists():
            print(f"{settings_file_path} already exists. Skipping generation.")
            return

        settings_data = {
            "arch": arch,
            "template": str(default_template),
            "runner": runner
        }

        with open(settings_file_path, 'w') as settings_file:
            yaml.dump(settings_data, settings_file, default_flow_style=False)
        print(f"Generated {settings_file_path}")

    def monitor_processes(self):
        while True:
            time.sleep(3)  # Increase the interval to give some time buffer
            finished_processes = []
            
            # Create a copy of the dictionary keys
            running_processes_keys = list(self.running_processes.keys())

            for script_stem in running_processes_keys:
                process_info = self.running_processes.get(script_stem)
                if process_info is None:
                    continue
                
                proc = process_info["proc"]
                if proc and proc.poll() is not None:
                    finished_processes.append(script_stem)
                else:
                    # Check with pgrep if process is still running
                    pgid = process_info.get("pgid")
                    exe_file = process_info.get("script").stem[:15]
                    if pgid is not None:
                        try:
                            os.killpg(pgid, 0)
                        except ProcessLookupError:
                            finished_processes.append(script_stem)
                    else:
                        try:
                            pgrep_output = subprocess.check_output(["pgrep", "-aif", exe_file]).decode()
                            if not pgrep_output or any(appname.lower() in line.lower() for line in pgrep_output.splitlines()):
                                finished_processes.append(script_stem)
                        except subprocess.CalledProcessError:
                            finished_processes.append(script_stem)

            for script_stem in finished_processes:
                GLib.idle_add(self.process_ended, script_stem)

    def check_running_processes_and_update_buttons(self):
        def update_buttons():
            try:
                running_processes_output = subprocess.check_output(["pgrep", "-aif", r"\.exe"]).decode()
                running_processes = running_processes_output.splitlines()

                scripts = self.find_python_scripts()
                for script in scripts:
                    try:
                        exe_file, wineprefix, progname, script_args = self.extract_yaml_info(script)
                        exe_name = Path(exe_file).stem[:15]
                        
                        is_running = any(exe_name in process for process in running_processes)
                        
                        if is_running:
                            GLib.idle_add(self.update_script_button_state, script.stem)
                    except Exception as e:
                        print(f"Error processing script {script}: {e}")
            except subprocess.CalledProcessError as e:
                print(f"Error checking running processes: {e}")
            
            return False  # Return False to stop the idle_add loop

        GLib.idle_add(update_buttons)

    def update_script_button_state(self, script_stem):
        button = self.find_button_by_script_stem(script_stem)
        if button:
            play_button = button.get_first_child().get_next_sibling()
            play_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
            play_button.set_child(play_icon)
            play_button.set_tooltip_text("Stop")

            self.running_processes[script_stem] = {
                "proc": None,
                "pid": None,
                "pgid": None,
                "play_button": play_button,
                "play_icon": play_icon,
                "options_button": button.get_first_child(),
                "gear_icon": Gtk.Image.new_from_icon_name("emblem-system-symbolic"),
                "script": next((s for s in self.find_python_scripts() if s.stem == script_stem), None)
            }



    def remove_symlinks_from_drive_c_users(self, template_dir):
        users_dir = template_dir / "drive_c" / "users"
        for user_dir in users_dir.iterdir():
            if user_dir.is_dir():
                for item in user_dir.iterdir():
                    if item.is_symlink():
                        target_path = item.resolve()
                        item.unlink()
                        item.mkdir(parents=True, exist_ok=True)
                        print(f"Replaced symlink with directory: {item}")

    def copy_template(self, prefix_dir):
        if not prefix_dir.exists():
            try:
                print(f"Copying default template to {prefix_dir}")
                shutil.copytree(default_template, prefix_dir, symlinks=True)
            except shutil.Error as e:
                for src, dst, err in e.args[0]:
                    if not os.path.exists(dst):
                        shutil.copy2(src, dst)
                    else:
                        print(f"Skipping {src} -> {dst} due to error: {err}")
        else:
            print(f"Prefix directory {prefix_dir} already exists. Skipping copying process.")

    def on_template_initialized(self):
        self.initializing_template = False
        if self.spinner:
            self.spinner.stop()
            self.button_box.remove(self.spinner)
            self.spinner = None

        box = self.open_button.get_child()
        child = box.get_first_child()
        while child:
            if isinstance(child, Gtk.Image):
                child.set_visible(True)
            elif isinstance(child, Gtk.Label):
                child.set_label("Open")
            child = child.get_next_sibling()

        if self.open_button_handler_id is not None:
            self.open_button_handler_id = self.open_button.connect("clicked", self.on_open_exe_clicked)

        print("Template initialization completed and UI updated.")

        # Process the CLI file if provided after template initialization
        if self.command_line_file:
            self.process_cli_file(self.command_line_file)

    def check_required_programs(self):
        # Check if flatpak-spawn is available
        if shutil.which("flatpak-spawn"):
            return []

        # Check other required programs if flatpak-spawn is not available
        required_programs = [
            'exiftool',
            'wine',
            'winetricks',
            'wrestool',
            'icotool',
            'pgrep',
            'gnome-terminal',
            'xdg-open'
        ]
        missing_programs = [prog for prog in required_programs if not shutil.which(prog)]
        return missing_programs

    def show_missing_programs_dialog(self, missing_programs):
        dialog = Gtk.Dialog(transient_for=self.window, modal=True)
        dialog.set_title("Missing Programs")
        dialog.set_default_size(300, 200)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        dialog.set_child(box)

        label = Gtk.Label(label="The following required programs are missing:")
        box.append(label)

        for prog in missing_programs:
            prog_label = Gtk.Label(label=prog)
            prog_label.set_halign(Gtk.Align.START)
            box.append(prog_label)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda w: dialog.close())
        box.append(close_button)

        dialog.present()

    def create_main_window(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Wine Charm")
        self.window.set_default_size(420, 560)
        self.window.add_css_class("common-background")
        
        self.headerbar = Gtk.HeaderBar()
        self.headerbar.set_show_title_buttons(True)
        self.headerbar.add_css_class("flat")
        self.window.set_titlebar(self.headerbar)

        app_icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        app_icon_box.set_margin_start(10)
        app_icon = Gtk.Image.new_from_icon_name("io.github.fastrizwaan.WineCharm")
        app_icon.set_pixel_size(18)  # Set icon size to 18
        app_icon_box.append(app_icon)
        self.headerbar.pack_start(app_icon_box)

        self.search_button = Gtk.ToggleButton()
        search_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        self.search_button.set_child(search_icon)
        self.search_button.connect("toggled", self.on_search_button_clicked)
        self.search_button.add_css_class("flat")
        self.headerbar.pack_start(self.search_button)

        self.menu_button = Gtk.MenuButton()
        menu_icon = Gtk.Image.new_from_icon_name("open-menu-symbolic")
        self.menu_button.set_child(menu_icon)
        self.menu_button.add_css_class("flat")

        self.menu_button.set_tooltip_text("Menu")
        self.headerbar.pack_end(self.menu_button)

        menu = Gio.Menu()
        for label, action in self.hamburger_actions:
            menu.append(label, f"app.{action.__name__}")
            action_item = Gio.SimpleAction.new(action.__name__, None)
            action_item.connect("activate", action)
            self.add_action(action_item)

        self.menu_button.set_menu_model(menu)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.vbox.set_margin_start(10)
        self.vbox.set_margin_end(10)
        self.vbox.set_margin_top(5)
        self.vbox.set_margin_bottom(10)
        self.window.set_child(self.vbox)

        self.button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.button_box.set_halign(Gtk.Align.CENTER)
        open_icon = Gtk.Image.new_from_icon_name("folder-open-symbolic")
        open_label = Gtk.Label(label="Open")

        self.button_box.append(open_icon)
        self.button_box.append(open_label)

        self.open_button = Gtk.Button()
        self.open_button.set_child(self.button_box)
        self.open_button.set_size_request(-1, 36)  # Set height to 40 pixels
        self.open_button_handler_id = self.open_button.connect("clicked", self.on_open_exe_clicked)
        self.vbox.append(self.open_button)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Search")
        self.search_entry.connect("activate", self.on_search_entry_activated)
        self.search_entry.connect("changed", self.on_search_entry_changed)

        self.search_entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        self.search_entry_box.append(self.search_entry)
        self.search_entry_box.set_hexpand(True)
        self.search_entry.set_hexpand(True)

        self.main_frame = Gtk.Frame()
        self.main_frame.set_margin_top(5)
        self.vbox.append(self.main_frame)

        self.scrolled = Gtk.ScrolledWindow()  # Make scrolled an instance variable
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.main_frame.set_child(self.scrolled)

        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_valign(Gtk.Align.START)
        self.flowbox.set_halign(Gtk.Align.FILL)
        self.flowbox.set_max_children_per_line(4)
        self.flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flowbox.set_vexpand(True)
        self.flowbox.set_hexpand(True)
        self.scrolled.set_child(self.flowbox)

        self.window.present()

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.window.add_controller(key_controller)


    def on_key_pressed(self, controller, keyval, keycode, state):
        print(f"Key pressed: {keyval}, state: {state}")
        if keyval == Gdk.KEY_f and (state & Gdk.ModifierType.CONTROL_MASK):
            print("Ctrl+F detected")
            self.activate_search_mode()
        elif keyval == Gdk.KEY_Escape:
            if self.search_active:
                print("Escape detected, deactivating search mode")
                self.deactivate_search_mode()
            elif not self.flowbox_state:
                print("Escape detected, going back")
                self.on_back_button_clicked(None)

    def activate_search_mode(self):
        print("Activating search mode")
        if self.open_button.get_parent() == self.vbox:
            self.vbox.remove(self.open_button)
        if self.search_entry_box.get_parent() is None:
            self.vbox.prepend(self.search_entry_box)
        self.search_entry.grab_focus()
        self.search_active = True

    def deactivate_search_mode(self):
        print("Deactivating search mode")
        if self.search_entry_box.get_parent() == self.vbox:
            self.vbox.remove(self.search_entry_box)
        if self.open_button.get_parent() is None:
            self.vbox.prepend(self.open_button)
        self.search_active = False
        self.filter_script_list("")  # Reset the list to show all scripts

    def on_search_button_clicked(self, button):
        print("Search button clicked")
        if self.search_active:
            self.deactivate_search_mode()
        else:
            self.activate_search_mode()

    def on_search_entry_activated(self, entry):
        search_term = entry.get_text().lower()
        self.filter_script_list(search_term)

    def reselect_previous_row(self):
        """
        Reselect the previously selected row in the script list, if any.
        """
        if self.selected_row:
            self.flowbox.select_child(self.selected_row)
            
    def on_search_entry_changed(self, entry):
        search_term = entry.get_text().lower()
        self.filter_script_list(search_term)

    def filter_script_list(self, search_term):
        scripts = self.find_python_scripts()

        # Remove all existing children
        child = self.flowbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.flowbox.remove(child)
            child = next_child

        # Add filtered scripts
        filtered_scripts = [script for script in scripts if search_term in script.stem.lower()]

        for script in filtered_scripts:
            button = self.create_script_button(script)
            self.flowbox.append(button)
            button.set_visible(True)
            

        self.reselect_previous_row()

    def on_open_exe_clicked(self, button):
        self.open_file_dialog()

    def open_file_dialog(self):
        file_dialog = Gtk.FileDialog.new()
        filter_model = Gio.ListStore.new(Gtk.FileFilter)
        filter_model.append(self.create_file_filter())
        file_dialog.set_filters(filter_model)
        file_dialog.open(self.window, None, self.on_file_dialog_response)

    def create_file_filter(self):
        file_filter = Gtk.FileFilter()
        file_filter.set_name("EXE and MSI files")
        file_filter.add_mime_type("application/x-ms-dos-executable")
        file_filter.add_pattern("*.exe")
        file_filter.add_pattern("*.msi")
        return file_filter

    def on_file_dialog_response(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                file_path = file.get_path()
                self.show_processing_spinner("Processing")
                threading.Thread(target=self.process_file, args=(file_path,)).start()
        except GLib.Error as e:
            if e.domain != 'gtk-dialog-error-quark' or e.code != 2:
                print(f"An error occurred: {e}")
        finally:
            self.window.set_visible(True)

    def show_processing_spinner(self, message="Processing"):
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.button_box.append(self.spinner)

        box = self.open_button.get_child()
        child = box.get_first_child()
        while child:
            if isinstance(child, Gtk.Image):
                child.set_visible(False)
            elif isinstance(child, Gtk.Label):
                child.set_label(message)
            child = child.get_next_sibling()

    def hide_processing_spinner(self):
        if self.spinner:
            self.spinner.stop()
            self.button_box.remove(self.spinner)

        box = self.open_button.get_child()
        child = box.get_first_child()
        while child:
            if isinstance(child, Gtk.Image):
                child.set_visible(True)
            elif isinstance(child, Gtk.Label):
                child.set_label("Open")
            child = child.get_next_sibling()

    def process_file(self, file_path):
        try:
            # Resolve the absolute path
            abs_file_path = str(Path(file_path).resolve())
            print(f"Resolved absolute file path: {abs_file_path}")  # Debugging output

            # Check if the file exists
            if not Path(abs_file_path).exists():
                print(f"File does not exist: {abs_file_path}")
                return

            self.create_yaml_file(abs_file_path, None)
            GLib.idle_add(self.create_script_list)
        except Exception as e:
            print(f"Error processing file: {e}")
        finally:
            GLib.idle_add(self.hide_processing_spinner)

    def on_hover_enter(self, button_box):
        button_box.set_opacity(1.0)

    def on_hover_leave(self, button_box):
        button_box.set_opacity(0.0)


    def create_script_list(self):
        # Remove all existing children
        while self.flowbox.get_child_at_index(0) is not None:
            child = self.flowbox.get_child_at_index(0)
            self.flowbox.remove(child)

        # Add new script buttons
        scripts = self.find_python_scripts()
        for script in scripts:
            button = self.create_script_button(script)

#            button.add_css_class("flat")
            self.flowbox.append(button)


    def clear_flowbox(self):
        child = self.flowbox.get_first_child()
        while child:
           next_child = child.get_next_sibling()
           self.flowbox.remove(child)
           child = next_child

    def find_lnk_files(self, wineprefix):
        drive_c = wineprefix / "drive_c"
        lnk_files = []

        for root, dirs, files in os.walk(drive_c):
            for file in files:
                file_path = Path(root) / file
                if file_path.suffix.lower() == ".lnk" and file_path.is_file():
                    lnk_files.append(file_path)

        return lnk_files

    def add_lnk_file_to_processed(self, wineprefix, lnk_file):
        found_lnk_files_path = wineprefix / "found_lnk_files.yaml"
        if found_lnk_files_path.exists():
            with open(found_lnk_files_path, 'r') as file:
                found_lnk_files = yaml.safe_load(file) or []
        else:
            found_lnk_files = []

        filename = lnk_file.name
        if filename not in found_lnk_files:
            found_lnk_files.append(filename)

        with open(found_lnk_files_path, 'w') as file:
            yaml.dump(found_lnk_files, file, default_flow_style=False)

    def is_lnk_file_processed(self, wineprefix, lnk_file):
        found_lnk_files_path = wineprefix / "found_lnk_files.yaml"
        if found_lnk_files_path.exists():
            with open(found_lnk_files_path, 'r') as file:
                found_lnk_files = yaml.safe_load(file) or []
                return lnk_file.name in found_lnk_files
        return False

    def create_scripts_for_lnk_files(self, wineprefix):
        lnk_files = self.find_lnk_files(wineprefix)
        exe_files = self.extract_exe_files_from_lnk(lnk_files, wineprefix)
        
        product_name_map = {}
        
        for exe_file in exe_files:
            product_name = self.get_product_name(exe_file)
            if product_name:
                if product_name not in product_name_map:
                    product_name_map[product_name] = []
                product_name_map[product_name].append(exe_file)
            else:
                self.create_yaml_file(exe_file, wineprefix)
        
        for product_name, exe_files in product_name_map.items():
            for exe_file in exe_files:
                if len(exe_files) > 1:
                    self.create_yaml_file(exe_file, wineprefix, use_exe_name=True)
                else:
                    self.create_yaml_file(exe_file, wineprefix, use_exe_name=False)

    def extract_exe_files_from_lnk(self, lnk_files, wineprefix):
        exe_files = []
        for lnk_file in lnk_files:
            if not self.is_lnk_file_processed(wineprefix, lnk_file):
                target_cmd = f'exiftool "{lnk_file}"'
                target_output = self.run_command(target_cmd)
                if target_output is None:
                    print(f"Error: Failed to retrieve target for {lnk_file}")
                    continue
                target_dos_name_match = re.search(r'Target File DOS Name\s+:\s+(.+)', target_output)
                target_dos_name = target_dos_name_match.group(1).strip() if target_dos_name_match else None
                if target_dos_name:
                    exe_name = target_dos_name.strip()
                    exe_path = self.find_exe_path(wineprefix, exe_name)
                    if exe_path and "unins" not in exe_path.stem.lower():
                        exe_files.append(exe_path)
                        self.add_lnk_file_to_processed(wineprefix, lnk_file)  # Track the .lnk file, not the .exe file
        return exe_files

    def run_command(self, command):
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"Error executing command: {e.stderr}")
            return None

    def get_product_name(self, exe_file):
        product_cmd = [
            'exiftool', shlex.quote(str(exe_file))
        ]

        product_output = self.run_command(" ".join(product_cmd))
        if product_output is None:
            print(f"Error: Failed to retrieve product name for {exe_file}")
            return None
        else:
            productname_match = re.search(r'Product Name\s+:\s+(.+)', product_output)
            return productname_match.group(1).strip() if productname_match else None

    def create_yaml_file(self, exe_path, prefix_dir=None, use_exe_name=False):
        exe_file = Path(exe_path).resolve()
        exe_name = exe_file.stem
        exe_no_space = exe_name.replace(" ", "_")

        sha256_hash = hashlib.sha256()
        with open(exe_file, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        sha256sum = sha256_hash.hexdigest()[:10]

        if prefix_dir is None:
            prefix_dir = prefixes_dir / f"{exe_no_space}-{sha256sum}"
            if not prefix_dir.exists():
                if default_template.exists():
                    self.copy_template(prefix_dir)
                else:
                    prefix_dir.mkdir(parents=True, exist_ok=True)
                    print(f"Created prefix directory: {prefix_dir}")

        wineprefix_name = prefix_dir.name

        product_cmd = [
            'exiftool', shlex.quote(str(exe_file))
        ]

        product_output = self.run_command(" ".join(product_cmd))
        if product_output is None:
            print(f"Error: Failed to retrieve product name for {exe_file}")
            productname = exe_no_space
        else:
            productname_match = re.search(r'Product Name\s+:\s+(.+)', product_output)
            productname = productname_match.group(1).strip() if productname_match else exe_no_space

        if use_exe_name or "setup" in exe_name.lower() or "install" in exe_name.lower():
            progname = exe_name
        elif use_exe_name or "setup" in productname.lower() or "install" in productname.lower():
            progname = productname
        else:
            progname = productname if productname and not any(char.isdigit() for char in productname) and productname.isascii() else exe_no_space

        yaml_data = {
            'exe_file': str(exe_file).replace(str(Path.home()), "~"),
            'wineprefix': str(prefix_dir).replace(str(Path.home()), "~"),
            'progname': progname,
            'args': "",
            'sha256sum': sha256_hash.hexdigest()  # Add sha256sum to YAML
        }

        yaml_file_path = prefix_dir / f"{progname.replace(' ', '_')}.charm"
        with open(yaml_file_path, 'w') as yaml_file:
            yaml.dump(yaml_data, yaml_file)

        icon_path = self.extract_icon(exe_file, prefix_dir, exe_no_space, progname)
        self.create_desktop_entry(progname, yaml_file_path, icon_path, prefix_dir)

        self.add_or_update_script_button(yaml_file_path)

    def extract_yaml_info(self, script):
        if not script.exists():
            raise FileNotFoundError(f"Script file not found: {script}")
        with open(script, 'r') as file:
            try:
                data = yaml.safe_load(file)
            except yaml.YAMLError as e:
                print(f"Error loading YAML file {script}: {e}")
                data = {}
        return (str(Path(data.get('exe_file', '')).expanduser().resolve()), 
                str(Path(data.get('wineprefix', '')).expanduser().resolve()), 
                data.get('progname', ''), 
                data.get('args', ''))

    def extract_icon(self, exe_file, wineprefix, exe_no_space, progname):
        icon_path = wineprefix / f"{progname.replace(' ', '_')}.png"
        tempdir.mkdir(parents=True, exist_ok=True)
        ico_path = tempdir / f"{exe_no_space}.ico"

        try:
            wrestool_cmd = f"wrestool -x -t 14 {shlex.quote(str(exe_file))} > {shlex.quote(str(ico_path))} 2> /dev/null"
            icotool_cmd = f"icotool -x {shlex.quote(str(ico_path))} -o {shlex.quote(str(tempdir))} 2> /dev/null"
            subprocess.run(wrestool_cmd, shell=True, check=True)
            subprocess.run(icotool_cmd, shell=True, check=True)

            png_files = sorted(tempdir.glob(f"{exe_no_space}*.png"), key=lambda x: x.stat().st_size, reverse=True)
            if png_files:
                best_png = png_files[0]
                shutil.move(best_png, icon_path)
            else:
                print("No PNG files extracted from ICO.")
        except subprocess.CalledProcessError as e:
            print(f"Error extracting icon: {e}")
        finally:
            for file in tempdir.glob(f"{exe_no_space}*"):
                file.unlink()
            tempdir.rmdir()

        return icon_path if icon_path.exists() else None

    def create_desktop_entry(self, progname, script_path, icon_path, wineprefix):
        desktop_file_content = f"""[Desktop Entry]
Name={progname}
Type=Application
Exec=python3 '{script_path}'
Icon={icon_path if icon_path else 'wine'}
Keywords=winecharm; game; {progname};
NoDisplay=false
StartupNotify=true
Terminal=false
Categories=Game;Utility;
"""
#       desktop_file_path = wineprefix / f"{progname}.desktop"
#       
#       with open(desktop_file_path, "w") as desktop_file:
#           desktop_file.write(desktop_file_content)

#       symlink_path = applicationsdir / f"{progname}.desktop"
#       if symlink_path.exists() or symlink_path.is_symlink():
#           symlink_path.unlink()
#       symlink_path.symlink_to(desktop_file_path)

#       if icon_path:
#           icon_symlink_path = iconsdir / f"{icon_path.name}"
#           if icon_symlink_path.exists() or symlink_path.is_symlink():
#               icon_symlink_path.unlink(missing_ok=True)
#           icon_symlink_path.symlink_to(icon_path)

    def add_or_update_script_button(self, script_path):
        script_stem = script_path.stem
        existing_button = self.find_button_by_script_stem(script_stem)
        
        if existing_button:
            self.flowbox.remove(existing_button)
            self.running_processes.pop(script_stem, None)

        button = self.create_script_button(script_path)
        button.add_css_class("new-script-button")
        self.flowbox.insert(button, 0)
        self.flowbox.select_child(button)
        self.new_scripts.add(script_stem)


    def embolden_new_scripts(self):
        child = self.flowbox.get_first_child()
        while child:
            script_label = child.get_first_child().get_first_child().get_next_sibling()
            if script_label and script_label.get_text().replace(" ", "_") in self.new_scripts:
                script_label.set_markup(f"<b>{script_label.get_text()}</b>")
                child.get_first_child().add_css_class("new-script-button")
            child = child.get_next_sibling()

    def on_back_button_clicked(self, button):
        print("Back button clicked")
        self.create_script_list()
        self.window.set_title("Wine Charm")
        self.headerbar.set_title_widget(None)
        self.menu_button.set_visible(True)
        self.search_button.set_visible(True)
        self.back_button.set_visible(False)

        if self.open_button.get_parent() != self.vbox:
            self.vbox.remove(self.open_button)
        if self.open_button.get_parent() is None:
            self.vbox.prepend(self.open_button)
        self.open_button.set_visible(True)

        if self.main_frame.get_child() != self.scrolled:
            self.main_frame.set_child(None)
            self.main_frame.set_child(self.scrolled)

        self.flowbox_state = True



    def show_about_dialog(self, action=None, param=None):
        about_dialog = Adw.AboutWindow(
            transient_for=self.window,
            application_name="WineCharm",
            application_icon="io.github.fastrizwaan.WineCharm",
            version=f"{version}",
            copyright="GNU General Public License (GPLv3+)",
            comments="A Charming Wine GUI Application",
            website="https://github.com/fastrizwaan/WineCharm",
            developer_name="Mohammed Asif Ali Rizvan",
            license_type=Gtk.License.GPL_3_0,
            issue_url="https://github.com/fastrizwaan/WineCharm/issues"
        )
        about_dialog.present()

    def on_settings_clicked(self, action, param):
        print("Settings action triggered")

    def on_kill_all_clicked(self, action=None, param=None):
        try:
            # Get the PID of the WineCharm application
            winecharm_pid_output = subprocess.check_output(["pgrep", "-aif", appname]).decode()
            winecharm_pid_lines = winecharm_pid_output.splitlines()
            winecharm_pids = [int(line.split()[0]) for line in winecharm_pid_lines]

            try:
                # Get the list of all Wine exe processes
                wine_exe_output = subprocess.check_output(["pgrep", "-aif", r"\.exe"]).decode()
                wine_exe_lines = wine_exe_output.splitlines()

                # Extract PIDs and reverse the list to kill child processes first
                pids = []
                for line in wine_exe_lines:
                    columns = line.split()
                    pid = int(columns[0])
                    if pid != 1 and pid not in winecharm_pids:  # Skip PID 1 and WineCharm PIDs
                        pids.append(pid)
                pids.reverse()

                # Kill the processes
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        print(f"Terminated process with PID: {pid}")
                    except ProcessLookupError:
                        print(f"Process with PID {pid} not found")
                    except PermissionError:
                        print(f"Permission denied to kill PID: {pid}")
            except subprocess.CalledProcessError:
                print("No matching Wine exe processes found.")

        except subprocess.CalledProcessError as e:
            print(f"Error retrieving process list: {e}")

        # Optionally, clear the running processes dictionary
        self.running_processes.clear()

        print("All Wine exe processes killed except PID 1 and WineCharm processes")

        # Updating script list so that stop buttons become play buttons
        self.create_script_list()

    def on_help_clicked(self, action, param):
        print("Help action triggered")

    def on_about_clicked(self, action, param):
        self.show_about_dialog()

    def quit_app(self, action, param):
        self.quit()

    def find_python_scripts(self):
        scripts = []
        for root, dirs, files in os.walk(prefixes_dir):
            depth = root[len(str(prefixes_dir)):].count(os.sep)
            if depth < 2:
                for file in files:
                    if file.endswith(".charm"):
                        scripts.append(Path(root) / file)
            else:
                dirs[:] = []
        scripts.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return scripts

    def find_exe_path(self, wineprefix, exe_name):
        drive_c = wineprefix / "drive_c"
        for root, dirs, files in os.walk(drive_c):
            for file in files:
                if file.lower() == exe_name.lower():
                    return Path(root) / file
        return None

    def update_running_script_buttons(self):
        for script_stem, process_info in self.running_processes.items():
            button = self.find_button_by_script_stem(script_stem)
            if button:
                play_button = button.get_first_child().get_next_sibling()
                play_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
                play_button.set_child(play_icon)
                play_button.set_tooltip_text("Stop")

        
    def find_button_by_script_path(self, script_path):
        child = self.flowbox.get_first_child()
        while child:
            script_label = child.get_first_child().get_first_child().get_next_sibling()
            if script_label and script_label.get_text().replace(" ", "_") == script_path.stem:
                return child
            child = child.get_next_sibling()
        return None

    def find_button_by_script_stem(self, script_stem):
        child = self.flowbox.get_first_child()
        while child:
            script_label = child.get_first_child().get_first_child().get_next_sibling()
            if script_label and script_label.get_text().replace(" ", "_") == script_stem:
                return child
            child = child.get_next_sibling()
        return None

    def load_icon(self, script):
        icon_name = script.stem + ".png"
        icon_dir = script.parent
        icon_path = icon_dir / icon_name
        default_icon_path = self.get_default_icon_path()

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_path), 32, 32)
            return Gdk.Texture.new_for_pixbuf(pixbuf)
        except Exception:
            print(f"Icon not found: {icon_name}")
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(default_icon_path), 32, 32)
                return Gdk.Texture.new_for_pixbuf(pixbuf)
            except Exception:
                print(f"Error loading default icon: {default_icon_path}")
                return None

    def get_default_icon_path(self):
        xdg_data_dirs = os.getenv("XDG_DATA_DIRS", "").split(":")
        icon_relative_path = "icons/hicolor/128x128/apps/org.winehq.Wine.png"
        
        for data_dir in xdg_data_dirs:
            icon_path = Path(data_dir) / icon_relative_path
            if icon_path.exists():
                return icon_path
        
        # Fallback icon path in case none of the paths in XDG_DATA_DIRS contain the icon
        return Path("/app/share/icons/hicolor/128x128/apps/org.winehq.Wine.png")

    def create_icon_title_widget(self, script):
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        icon = self.load_icon(script)
        icon_image = Gtk.Image.new_from_paintable(icon)
        icon_image.set_pixel_size(24)
        hbox.append(icon_image)

        label = Gtk.Label(label=f"<b>{script.stem.replace('_', ' ')}</b>")
        label.set_use_markup(True)
        hbox.append(label)

        return hbox
        
    def create_script_button(self, script):
        flowbox_child = Gtk.FlowBoxChild()

        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("normal-font")
        button.set_size_request(200, 36)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        button.set_child(hbox)

        icon = self.load_icon(script)
        icon_image = Gtk.Image.new_from_paintable(icon)
        icon_image.set_pixel_size(32)
        hbox.append(icon_image)
        icon_image.set_visible(True)

        label_text = script.stem.replace("_", " ")
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_hexpand(True)
        label.set_vexpand(False)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        hbox.append(label)
        label.set_visible(True)

        button.label = label
        button.connect("clicked", lambda btn: self.show_options_for_script(script, button))

        flowbox_child.set_child(button)
        self.flowbox_state = True
        return flowbox_child

    def on_hover_button_clicked(self, event):
        # Handle the click event for the hover buttons
        print("Hover button clicked")
        # Add your custom logic here

        
    def stop_script(self, process_info):
        if process_info:
            proc = process_info["proc"]
            pid = process_info["pid"]
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                print(f"Terminated wine process with PID {pid}")
                play_button = process_info["play_button"]
                play_icon = process_info["play_icon"]
                options_button = process_info["options_button"]
                gear_icon = process_info["gear_icon"]
                script = process_info["script"]
                self.reset_buttons(play_button, play_icon, options_button, gear_icon, script)
            except ProcessLookupError as e:
                print(f"Error terminating wine process: {e}")
        else:
            print("No running process found")

    def launch_script(self, script, play_stop_button, button):
        print(f"Launching script {script}")
        exe_file, wineprefix, progname, script_args = self.extract_yaml_info(script)
        exe_name = Path(exe_file).name
        exe_dir = Path(exe_file).parent
        
        # Check if the exe_file exists
        if not Path(exe_file).exists():
            self.show_exe_not_found_dialog(exe_name, exe_file, script, play_stop_button, button)
            return

        command = f"cd {shlex.quote(str(exe_dir))} && WINEPREFIX={shlex.quote(str(wineprefix))} wine {shlex.quote(str(Path(exe_file).name))} {script_args}"

        try:
            initial_lnk_files = self.find_lnk_files(Path(wineprefix))
            proc = subprocess.Popen(
                command,
                shell=True,
                preexec_fn=os.setsid,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            pgid = os.getpgid(proc.pid)

            print(f"Launched script with PID: {proc.pid}")

            self.running_processes[script.stem] = {
                "proc": proc,
                "pid": proc.pid,
                "pgid": pgid,
                "play_button": play_stop_button,
                "play_icon": Gtk.Image.new_from_icon_name("media-playback-stop-symbolic"),
                "options_button": button.get_first_child(),
                "gear_icon": Gtk.Image.new_from_icon_name("emblem-system-symbolic"),
                "script": script
            }

            def monitor_process():
                proc.wait()
                GLib.idle_add(self.process_ended, script.stem)
                GLib.idle_add(self.create_scripts_for_lnk_files, Path(wineprefix))
                time.sleep(5)
                GLib.idle_add(self.update_related_scripts_buttons, Path(wineprefix))

            threading.Thread(target=monitor_process).start()

            # Use the new method here
            self.update_running_script_buttons()

        except Exception as e:
            print(f"Error launching script: {e}")

    def update_related_scripts_buttons(self, wineprefix, script_stem_to_select=None):
        scripts = self.find_python_scripts()
        for script in scripts:
            script_exe_file, script_wineprefix, _, _ = self.extract_yaml_info(script)
            if script_wineprefix == str(wineprefix):
                exe_name_upto_fifteen_chars = Path(script_exe_file).stem[:15]
                try:
                    pgrep_output = subprocess.check_output(["pgrep", "-ai", exe_name_upto_fifteen_chars]).decode()
                    if pgrep_output:
                        button = self.find_button_by_script_stem(Path(script).stem)
                        if button:
                            play_button = button.get_first_child().get_next_sibling()
                            play_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
                            play_button.set_child(play_icon)
                            play_button.set_tooltip_text("Stop")
                            self.running_processes[script.stem] = {
                                "proc": None,
                                "pid": None,
                                "pgid": None,
                                "play_button": play_button,
                                "play_icon": play_icon,
                                "options_button": button.get_first_child(),
                                "gear_icon": Gtk.Image.new_from_icon_name("emblem-system-symbolic"),
                                "script": script
                            }
                            self.flowbox.select_child(button)
                except subprocess.CalledProcessError:
                    continue

    def disconnect_play_stop_handler(self, button):
        if button in self.play_stop_handlers:
            handler_id = self.play_stop_handlers.pop(button)
            button.disconnect(handler_id)

    def reset_buttons(self, play_button, play_icon, script):
        play_button.set_child(play_icon)
        play_button.set_sensitive(True)
        self.disconnect_play_stop_handler(play_button)
        self.play_stop_handlers[play_button] = play_button.connect("clicked", lambda btn: self.toggle_play_stop(script, play_button, self.find_button_by_script_stem(script.stem)))


    def select_button(self, button):
        self.flowbox.select_child(button)

    def delete_setup_or_install_script(self, script, button):
        try:
            script.unlink()
            # self.create_script_list()
        except Exception as e:
            print(f"Error deleting script: {e}")

    def create_button_box(self, script, button):
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_margin_end(0)

        play_stop_button = Gtk.Button()
        play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        play_stop_button.set_child(play_icon)
        play_stop_button.set_tooltip_text("Play")
        self.play_stop_handlers[play_stop_button] = play_stop_button.connect("clicked", lambda btn: self.toggle_play_stop(script, play_stop_button, button))
        button_box.append(play_stop_button)

        script_name_contains_setup_or_install = "setup" in script.stem.lower() or "install" in script.stem.lower()
        script_prefix = script.parent
        prefix_scripts = [f for f in script_prefix.iterdir() if f.suffix == '.charm']

        if script_name_contains_setup_or_install and len(prefix_scripts) > 1:
            delete_button = Gtk.Button()
            delete_icon = Gtk.Image.new_from_icon_name("edit-delete-symbolic")
            delete_button.set_child(delete_icon)
            delete_button.set_tooltip_text("Delete Shortcut")
            delete_button.connect("clicked", lambda btn: self.delete_setup_or_install_script(script, button))
            button_box.append(delete_button)

        button_box.set_opacity(0.0)
        return button_box


        
    def delete_setup_or_install_script(self, script, button):
        print(f"Deleting setup or install script: {script}")
        try:
            script.unlink()
            self.flowbox.remove(button)
        except Exception as e:
            print(f"Error deleting script: {e}")

    def show_options_for_script(self, script, button):
        self.search_button.set_active(False)
        self.flowbox_state = False
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_vexpand(True)

        options_flowbox = Gtk.FlowBox()
        #options_flowbox.set_margin_start(5)
        #options_flowbox.set_margin_end(5)
        #options_flowbox.set_margin_top(5)
        #options_flowbox.set_margin_bottom(5)
        options_flowbox.set_max_children_per_line(1)
        #self.flowbox.set_max_children_per_line(8)
        options_flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled_window.set_child(options_flowbox)

        self.main_frame.set_child(scrolled_window)

        options = [
            ("Launch", "media-playback-start-symbolic", self.toggle_play_stop, "Run or stop the script"),
            ("Open Terminal", "utilities-terminal-symbolic", self.open_terminal, "Open a terminal in the script directory"),
            ("Install dxvk vkd3d", "emblem-system-symbolic", self.install_dxvk_vkd3d, "Install dxvk and vkd3d using winetricks"),
            ("Open Filemanager", "system-file-manager-symbolic", self.open_filemanager, "Open the file manager in the script directory"),
            ("Delete Wineprefix", "edit-delete-symbolic", self.show_delete_confirmation, "Delete the Wineprefix associated with the script"),
            ("Delete Shortcut", "edit-delete-symbolic", self.show_delete_shortcut_confirmation, "Show confirmation for deleting the shortcut for the script"),
            ("Wine Arguments", "preferences-system-symbolic", self.show_wine_arguments, "Set Wine Arguments")
        ]

        for label, icon_name, callback, tooltip in options:
            option_button = Gtk.Button()
            option_button.set_size_request(100, 36)

            option_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

            option_button.set_child(option_hbox)

            option_icon = Gtk.Image.new_from_icon_name(icon_name)
            option_label = Gtk.Label(label=label)
            option_label.set_tooltip_text(tooltip)
            option_label.set_xalign(0)
            option_label.set_hexpand(True)
            option_label.set_ellipsize(Pango.EllipsizeMode.END)
            option_hbox.append(option_icon)
            option_hbox.append(option_label)

            option_button.connect("clicked", lambda btn, cb=callback, sc=script: cb(sc, option_button, button))
            options_flowbox.append(option_button)

        self.headerbar.set_title_widget(self.create_icon_title_widget(script))
        self.menu_button.set_visible(False)
        self.search_button.set_visible(False)

        if self.back_button.get_parent():
            self.headerbar.remove(self.back_button)
        self.headerbar.pack_start(self.back_button)
        self.back_button.set_visible(True)
        self.open_button.set_visible(False)

        self.update_execute_button_icon(script)
        self.selected_row = None
        
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.window.add_controller(key_controller)

    def toggle_play_stop(self, script, play_stop_button, button):
        if script.stem in self.running_processes:
            print(f"Stopping script: {script.stem}")
            self.terminate_script(script)
            stop_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            play_stop_button.set_child(stop_icon)
            play_stop_button.set_tooltip_text("Play")
        else:
            print(f"Starting script: {script.stem}")
            self.launch_script(script, play_stop_button, button)
            start_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
            play_stop_button.set_child(start_icon)
            play_stop_button.set_tooltip_text("Stop")

    def install_dxvk_vkd3d(self, script, option_button, button):
        wineprefix = script.parent
        self.run_winetricks_script("vkd3d dxvk", wineprefix)
        self.create_script_list()

    def show_delete_confirmation(self, script, option_button, button):
        original_content = option_button.get_child()

        delete_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        delete_hbox.set_margin_start(2)
        delete_hbox.set_margin_end(2)
        delete_hbox.set_margin_top(2)
        delete_hbox.set_margin_bottom(2)

        delete_label = Gtk.Label(label="Delete Wineprefix?")
        delete_hbox.append(delete_label)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.END)
        delete_hbox.append(button_box)

        yes_button = Gtk.Button(label="Yes")
        yes_button.add_css_class("destructive-action")
        yes_button.connect("clicked", lambda btn: self.delete_wineprefix(script, original_content, option_button))
        button_box.append(yes_button)

        no_button = Gtk.Button(label="No")
        no_button.connect("clicked", lambda btn: option_button.set_child(original_content))
        button_box.append(no_button)

        option_button.set_child(delete_hbox)

    def show_delete_shortcut_confirmation(self, script, option_button, button):
        original_content = option_button.get_child()

        delete_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        delete_hbox.set_margin_start(2)
        delete_hbox.set_margin_end(2)
        delete_hbox.set_margin_top(2)
        delete_hbox.set_margin_bottom(2)

        delete_label = Gtk.Label(label="Delete Shortcut?")
        delete_hbox.append(delete_label)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.END)
        delete_hbox.append(button_box)

        yes_button = Gtk.Button(label="Yes")
        yes_button.add_css_class("destructive-action")
        yes_button.connect("clicked", lambda btn: self.delete_shortcut(script, original_content, option_button))
        button_box.append(yes_button)

        no_button = Gtk.Button(label="No")
        no_button.connect("clicked", lambda btn: option_button.set_child(original_content))
        button_box.append(no_button)

        option_button.set_child(delete_hbox)

    def show_wine_arguments(self, script, option_button, button):
        dialog = Gtk.Dialog(transient_for=self.window, modal=True)
        dialog.set_title("Wine Arguments")
        dialog.set_default_size(300, 100)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        dialog.set_child(box)

        label = Gtk.Label(label="Wine Arguments")
        box.append(label)

        entry = Gtk.Entry()
        exe_file, wineprefix, progname, script_args = self.extract_yaml_info(script)
        entry.set_text(script_args or "-opengl -SkipBuildPatchPrereq")
        box.append(entry)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(button_box)

        save_button = Gtk.Button(label="Save")
        save_button.connect("clicked", lambda btn: self.save_wine_arguments(script, entry.get_text(), dialog))
        button_box.append(save_button)

        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda btn: dialog.close())
        button_box.append(cancel_button)

        dialog.present()


    def on_option_button_clicked(self, callback, script, row):
        self.options_listbox.select_row(row)
        callback(script)
          
    def terminate_script(self, script):
        print(f"Terminating script: {script}")
        if script.stem in self.running_processes:
            process_info = self.running_processes.pop(script.stem, None)
            if process_info:
                exe_file, wineprefix, _, _ = self.extract_yaml_info(script)
                exe_name_upto_fifteen_chars = Path(exe_file).stem[:15]

                try:
                    pgrep_output = subprocess.check_output(["pgrep", "-aif", exe_name_upto_fifteen_chars]).decode()
                    pids_to_kill = [line.split()[0] for line in pgrep_output.splitlines()]

                    winecharm_pid_output = subprocess.check_output(["pgrep", "-aif", appname]).decode()
                    winecharm_pid_lines = winecharm_pid_output.splitlines()
                    winecharm_pids = [int(line.split()[0]) for line in winecharm_pid_lines]

                    for pid in pids_to_kill:
                        if int(pid) in winecharm_pids:
                            print(f"Skipping termination of {appname} process with PID: {pid}")
                            continue

                        try:
                            os.kill(int(pid), signal.SIGKILL)
                            print(f"Terminated PID: {pid}")
                        except ProcessLookupError:
                            continue

                        print(f"Terminated wine process for {script}")
                        self.reset_buttons(
                            process_info["play_button"],
                            process_info["play_icon"],
                            script
                        )

                        self.update_related_scripts_buttons(wineprefix)
                except subprocess.CalledProcessError:
                    print(f"No processes found for {exe_name_upto_fifteen_chars}")
            else:
                print(f"No running process found for {script}")
        else:
            print(f"No running process found for {script}")

    def process_ended(self, script_stem):
        if script_stem in self.running_processes:
            process_info = self.running_processes.pop(script_stem)
            play_button = process_info["play_button"]
            play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            play_button.set_child(play_icon)
            play_button.set_tooltip_text("Play")

            self.disconnect_play_stop_handler(play_button)
            self.play_stop_handlers[play_button] = play_button.connect("clicked", lambda btn: self.toggle_play_stop(process_info["script"], play_button, self.find_button_by_script_stem(script_stem)))

            exe_file, wineprefix, _, _ = self.extract_yaml_info(process_info["script"])

            self.update_related_scripts_buttons(wineprefix)

    def update_execute_button_icon(self, script):
        if self.options_listbox:
            for row in self.options_listbox:
                box = row.get_child()
                for widget in box:
                    if isinstance(widget, Gtk.Button) and widget.get_tooltip_text() == "Run or stop the script":
                        play_stop_button = widget
                        if script.stem in self.running_processes:
                            play_stop_button.set_child(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic"))
                            play_stop_button.set_tooltip_text("Stop")
                        else:
                            play_stop_button.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
                            play_stop_button.set_tooltip_text("Run or stop the script")


    def save_wine_arguments(self, script, args, dialog):
        with open(script, 'r') as file:
            data = yaml.safe_load(file)
        data['args'] = args
        with open(script, 'w') as file:
            yaml.safe_dump(data, file)
        dialog.close()
        print(f"Saved arguments for {script}: {args}")

    def on_options_row_selected(self, listbox, row):
        pass

    def open_terminal(self, script, *args):
        wineprefix = Path(script).parent
        print(f"Opening terminal for {wineprefix}")
        if not wineprefix.exists():
            wineprefix.mkdir(parents=True, exist_ok=True)

        if shutil.which("flatpak-spawn"):
            command = [
                "flatpak-spawn",
                "--host",
                "gnome-terminal",
                "--wait",
                "--",
                "flatpak",
                "--filesystem=host",
                "--filesystem=~/.var/app",
                "--command=bash",
                "run",
                "io.github.fastrizwaan.WineCharm",
                "--norc",
                "-c",
                rf'export PS1="[\u@\h:\w]\\$ "; export WINEPREFIX={shlex.quote(str(wineprefix))}; cd {shlex.quote(str(wineprefix))}; exec bash --norc -i'
            ]
        else:
            command = [
                "gnome-terminal",
                "--wait",
                "--",
                "bash",
                "--norc",
                "-c",
                rf'export PS1="[\u@\h:\w]\\$ "; export WINEPREFIX={shlex.quote(str(wineprefix))}; cd {shlex.quote(str(wineprefix))}; exec bash --norc -i'
            ]
        try:
            subprocess.Popen(command)
        except Exception as e:
            print(f"Error opening terminal: {e}")

    def open_filemanager(self, script, *args):
        wineprefix = Path(script).parent
        print(f"Opening file manager for {wineprefix}")
        command = ["xdg-open", str(wineprefix)]
        try:
            subprocess.Popen(command)
        except Exception as e:
            print(f"Error opening file manager: {e}")

    def delete_shortcut(self, script, original_content, button):
        try:
            script.unlink()
            print(f"Shortcut deleted: {script}")
            button.set_child(original_content)
            self.on_back_button_clicked(None)
        except Exception as e:
            print(f"Error deleting shortcut: {e}")

    def delete_wineprefix(self, script, original_content, button):
        wineprefix = Path(script).parent
        print(f"Deleting wineprefix: {wineprefix}")

        try:
            shutil.rmtree(wineprefix)
            print(f"Wineprefix deleted: {wineprefix}")
            button.set_child(original_content)
            self.on_back_button_clicked(None)
        except Exception as e:
            print(f"Error deleting wineprefix: {e}")

    def run_winetricks_script(self, winetricks_cmd, wineprefix):
        cmd = f"flatpak run --command=sh io.github.fastrizwaan.WineCharm -c 'WINEPREFIX={shlex.quote(str(wineprefix))} winetricks {winetricks_cmd}'"
        try:
            subprocess.run(cmd, shell=True, check=True)
            print(f"Executed winetricks {winetricks_cmd} for {wineprefix}")
        except subprocess.CalledProcessError as e:
            print(f"Error executing winetricks {winetricks_cmd}: {e}")

    def handle_sigint(self, signum, frame):
        print("Received SIGINT. Cleaning up and exiting.")
        if os.path.exists(SOCKET_FILE):
            os.remove(SOCKET_FILE)
        self.quit()

    def calculate_sha256sum(self, file_path):
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def clear_initialization_list(self):
        if self.flowbox:
            child = self.flowbox.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                self.flowbox.remove(child)
                child = next_child

    def copy_wine_directory(self, src, dst):
        try:
            self.custom_copytree(src, dst)
            print(f"Successfully copied Wine directory to {dst}")
            
            self.process_reg_files(dst)
            
            print(f"Creating scripts for .lnk files in {dst}")
            self.create_scripts_for_lnk_files(dst)
            print(f"Scripts created for .lnk files in {dst}")

            print(f"Creating scripts for .exe files in {dst}")
            self.create_scripts_for_exe_files(dst)
            print(f"Scripts created for .exe files in {dst}")

            GLib.idle_add(self.create_script_list)
        finally:
            GLib.idle_add(self.enable_open_button)
            GLib.idle_add(self.hide_processing_spinner)
            print("Completed importing Wine directory process.")

    def create_scripts_for_exe_files(self, wineprefix):
        exe_files = self.find_exe_files(wineprefix)
        for exe_file in exe_files:
            self.create_yaml_file(exe_file, wineprefix, use_exe_name=True)

    def custom_copytree(self, src, dst):
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.islink(s):
                linkto = os.readlink(s)
                os.symlink(linkto, d)
            elif os.path.isdir(s):
                self.custom_copytree(s, d)
            else:
                shutil.copy2(s, d)

    def disable_open_button(self):
        if self.open_button:
            self.open_button.set_sensitive(False)
        print("Open button disabled.")

    def enable_open_button(self):
        if self.open_button:
            self.open_button.set_sensitive(True)
        print("Open button enabled.")

    def find_exe_files(self, wineprefix):
        drive_c = Path(wineprefix) / "drive_c"
        exclude_patterns = [
            "windows", "dw20.exe", "BsSndRpt*.exe", "Rar.exe", "tdu2k.exe",
            "python.exe", "pythonw.exe", "zsync.exe", "zsyncmake.exe", "RarExtInstaller.exe",
            "UnRAR.exe", "wmplayer.exe", "iexplore.exe", "LendaModTool.exe", "netfx*.exe",
            "wordpad.exe", "quickSFV*.exe", "UnityCrashHand*.exe", "CrashReportClient.exe",
            "installericon.exe", "dwtrig*.exe", "ffmpeg*.exe", "ffprobe*.exe", "dx*setup.exe",
            "*vshost.exe", "*mgcb.exe", "cls-lolz*.exe", "cls-srep*.exe", "directx*.exe",
            "UnrealCEFSubProc*.exe", "UE4PrereqSetup*.exe", "dotnet*.exe", "oalinst.exe",
            "*redist*.exe", "7z*.exe", "unins*.exe"
        ]
        
        exe_files_found = []

        for root, dirs, files in os.walk(drive_c):
            dirs[:] = [d for d in dirs if not fnmatch.fnmatch(d, "windows")]
            for file in files:
                file_path = Path(root) / file
                if any(fnmatch.fnmatch(file, pattern) for pattern in exclude_patterns):
                    continue
                if file_path.suffix.lower() == ".exe" and file_path.is_file():
                    exe_files_found.append(file_path)

        return exe_files_found

    def get_wine_username(self, wineprefix):
        user_reg_path = os.path.join(wineprefix, "user.reg")
        if not os.path.exists(user_reg_path):
            print(f"File not found: {user_reg_path}")
            return None
        
        print(f"Reading user.reg file from {user_reg_path}")
        with open(user_reg_path, 'r') as file:
            content = file.read()
        
        match = re.search(r'"USERNAME"="([^"]+)"', content, re.IGNORECASE)
        if not match:
            print("Unable to determine the USERNAME from user.reg.")
            return None
        
        return match.group(1)

    def import_wine_directory(self, action, param):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Wine Directory")
        dialog.set_action(Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.connect("response", self.on_import_directory_response)
        dialog.present()

    def locate_exe_file(self, dialog, exe_file, script, play_stop_button, button):
        dialog.close()
        
        file_dialog = Gtk.FileDialog.new()
        filter_model = Gio.ListStore.new(Gtk.FileFilter)
        filter_model.append(self.create_file_filter())
        file_dialog.set_filters(filter_model)
        file_dialog.open(self.window, None, lambda dlg, res: self.on_locate_file_dialog_response(dlg, res, exe_file, script, play_stop_button, button))

    def on_import_directory_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            file = dialog.get_file()
            if file:
                directory = file.get_path()
                print(f"Selected directory: {directory}")
                if directory and (Path(directory) / "system.reg").exists():
                    print(f"Valid Wine directory selected: {directory}")
                    self.show_processing_spinner(f"Importing {Path(directory).name}")
                    self.disable_open_button()

                    dest_dir = prefixes_dir / Path(directory).name
                    print(f"Copying Wine directory to: {dest_dir}")
                    threading.Thread(target=self.copy_wine_directory, args=(directory, dest_dir)).start()
                else:
                    print(f"Invalid directory selected: {directory}")
                    self.show_error_dialog("Invalid Directory", "The selected directory does not appear to be a valid Wine directory.")
        dialog.destroy()
        print("FileChooserDialog destroyed.")

    def on_import_wine_directory_clicked(self, action, param):
        dialog = Gtk.FileChooserDialog(
            title="Select Wine Directory",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            transient_for=self.window,
            modal=True
        )
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "Open", Gtk.ResponseType.OK
        )
        dialog.connect("response", self.on_import_directory_response)
        dialog.present()
        print("FileChooserDialog presented for importing Wine directory.")

    def on_locate_file_dialog_response(self, dialog, result, original_exe_file, script, play_stop_button, button):
        wineprefix = self.extract_yaml_info(script)
        try:
            file = dialog.open_finish(result)
            if file:
                file_path = file.get_path()
                if file_path:
                    new_sha256sum = self.calculate_sha256sum(file_path)
                    with open(script, 'r') as file:
                        data = yaml.safe_load(file)
                    original_sha256sum = data.get('sha256sum')
                    
                    if new_sha256sum == original_sha256sum:
                        self.update_exe_file_path(script, file_path)
                        self.toggle_play_stop(script, play_stop_button, button)
                        GLib.idle_add(self.update_related_scripts_buttons, Path(wineprefix))
                    else:
                        self.show_error_dialog("Error", "The selected file does not match the original executable.")
        except GLib.Error as e:
            print(f"An error occurred: {e}")
        finally:
            self.window.set_visible(True)
            self.create_script_list()

    def on_template_initialized(self):
        self.initializing_template = False
        if self.spinner:
            self.spinner.stop()
            self.button_box.remove(self.spinner)
            self.spinner = None

        self.set_open_button_label("Open")

        checkbox, label = self.show_initializing_step("Initialization Complete!")
        GLib.idle_add(checkbox.set_active, True)

        if self.open_button_handler_id is not None:
            self.open_button_handler_id = self.open_button.connect("clicked", self.on_open_exe_clicked)
        print("Template initialization completed and UI updated.")

        # Process the CLI file if provided after template initialization
        if self.command_line_file:
            self.process_cli_file(self.command_line_file)

    def process_reg_files(self, wineprefix):
        print(f"Starting to process .reg files in {wineprefix}")
        
        # Get current username from the environment
        current_username = os.getenv("USERNAME") or os.getenv("USER")
        if not current_username:
            print("Unable to determine the current username from the environment.")
            return
        print(f"Current username: {current_username}")

        # Read the USERNAME from user.reg
        user_reg_path = os.path.join(wineprefix, "user.reg")
        if not os.path.exists(user_reg_path):
            print(f"File not found: {user_reg_path}")
            return
        
        print(f"Reading user.reg file from {user_reg_path}")
        with open(user_reg_path, 'r') as file:
            content = file.read()
        
        match = re.search(r'"USERNAME"="([^"]+)"', content, re.IGNORECASE)
        if not match:
            print("Unable to determine the USERNAME from user.reg.")
            return
        
        wine_username = match.group(1)
        print(f"Found USERNAME in user.reg: {wine_username}")

        # Define replacements
        replacements = {
            f"\\\\users\\\\{wine_username}": f"\\\\users\\\\{current_username}",
            f"\\\\home\\\\{wine_username}": f"\\\\home\\\\{current_username}",
            f'"USERNAME"="{wine_username}"': f'"USERNAME"="{current_username}"'
        }
        print("Defined replacements:")
        for old, new in replacements.items():
            print(f"  {old} -> {new}")

        # Process all .reg files in the wineprefix
        for root, dirs, files in os.walk(wineprefix):
            for file in files:
                if file.endswith(".reg"):
                    file_path = os.path.join(root, file)
                    print(f"Processing {file_path}")
                    
                    with open(file_path, 'r') as reg_file:
                        reg_content = reg_file.read()
                    
                    for old, new in replacements.items():
                        if old in reg_content:
                            reg_content = reg_content.replace(old, new)
                            print(f"Replaced {old} with {new} in {file_path}")
                        else:
                            print(f"No instances of {old} found in {file_path}")

                    with open(file_path, 'w') as reg_file:
                        reg_file.write(reg_content)
                    print(f"Finished processing {file_path}")

        print(f"Completed processing .reg files in {wineprefix}")

    def rename_user_directory(self, wineprefix, old_username):
        print(f"Renaming user directory in {wineprefix}")
        
        current_username = os.getenv("USERNAME") or os.getenv("USER")
        if not current_username:
            print("Unable to determine the current username from the environment.")
            return
        
        user_dir_old = os.path.join(wineprefix, "drive_c", "users", old_username)
        user_dir_new = os.path.join(wineprefix, "drive_c", "users", current_username)
        
        if os.path.exists(user_dir_old):
            os.rename(user_dir_old, user_dir_new)
            print(f"Renamed user directory from {user_dir_old} to {user_dir_new}")
        else:
            print(f"User directory {user_dir_old} does not exist.")

    def set_open_button_label(self, text):
        box = self.open_button.get_child()
        child = box.get_first_child()
        while child:
            if isinstance(child, Gtk.Label):
                child.set_label(text)
            elif isinstance(child, Gtk.Image):
                child.set_visible(False if text == "Initializing" else True)
            child = child.get_next_sibling()

    def show_error_dialog(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title
        )
        dialog.props.secondary_text = message
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.present()

    def show_exe_not_found_dialog(self, exe_name, exe_file, script, play_stop_button, button):
        dialog = Gtk.Dialog(
            transient_for=self.window,
            modal=True,
            title=f"{exe_name} not found"
        )
        dialog.set_default_size(300, 150)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        
        label_primary = Gtk.Label(label=f"{exe_name} not found at {Path(exe_file).parent}")
        label_secondary = Gtk.Label(label="Would you like to locate the executable file or cancel the operation?")
        label_primary.set_wrap(True)
        label_secondary.set_wrap(True)
        
        box.append(label_primary)
        box.append(label_secondary)
        
        locate_button = Gtk.Button(label=f"Locate {exe_name}")
        locate_button.connect("clicked", lambda btn: self.locate_exe_file(dialog, exe_file, script, play_stop_button, button))
        box.append(locate_button)
        
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda btn: dialog.close())
        box.append(cancel_button)
        
        dialog.set_child(box)
        dialog.present()

    def clear_and_replace_flowbox(self, halign=Gtk.Align.FILL, max_children_per_line=1):
        if self.flowbox:
            child = self.flowbox.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                self.flowbox.remove(child)
                child = next_child
        self.flowbox.set_halign(halign)
        self.flowbox.set_max_children_per_line(max_children_per_line)

    def initialize_template(self, template_dir, callback):
        if not template_dir.exists():
            self.initializing_template = True
            if self.open_button_handler_id is not None:
                self.open_button.disconnect(self.open_button_handler_id)

            self.spinner = Gtk.Spinner()
            self.spinner.start()
            self.button_box.append(self.spinner)

            self.set_open_button_label("Initializing")

            template_dir.mkdir(parents=True, exist_ok=True)

            steps = [
                ("Initializing wineprefix", f"WINEPREFIX='{template_dir}' WINEDEBUG=-all wineboot -i"),
                ("Installing vkd3d", f"WINEPREFIX='{template_dir}' winetricks vkd3d"),
                ("Installing dxvk", f"WINEPREFIX='{template_dir}' winetricks dxvk"),
                ("Installing corefonts", f"WINEPREFIX='{template_dir}' winetricks corefonts"),
                ("Installing openal", f"WINEPREFIX='{template_dir}' winetricks openal"),
            ]

            def initialize():
                for step_text, command in steps:
                    checkbox, label = self.show_initializing_step(step_text)
                    GLib.idle_add(label.set_markup, f"<b>{step_text}</b>")
                    try:
                        subprocess.run(command, shell=True, check=True)
                        GLib.idle_add(checkbox.set_active, True)
                    except subprocess.CalledProcessError as e:
                        print(f"Error initializing template: {e}")
                        break
                    finally:
                        GLib.idle_add(label.set_markup, step_text)
                GLib.idle_add(callback)

            thread = threading.Thread(target=initialize)
            thread.start()

    def show_initializing_step(self, step):
        button = Gtk.Button()
        button.set_size_request(200, 36)
        button.add_css_class("flat")
        button.add_css_class("normal-font")
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        checkbox = Gtk.CheckButton()
        label = Gtk.Label(label=step)
        label.set_xalign(0)
        hbox.append(checkbox)
        hbox.append(label)
        button.set_child(hbox)
        button.checkbox = checkbox
        button.label = label
        self.flowbox.set_max_children_per_line(4)
        self.flowbox.append(button)
        button.set_visible(True)
        return checkbox, label



    def update_exe_file_path(self, script, new_file_path):
        with open(script, 'r') as file:
            data = yaml.safe_load(file)
        data['exe_file'] = new_file_path
        with open(script, 'w') as file:
            yaml.safe_dump(data, file)


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="WineCharm GUI application")
    parser.add_argument('file', nargs='?', help="Path to the .exe or .msi file")
    return parser.parse_args()

def main():
    args = parse_args()

    app = WineCharmApp()

    if args.file:
        if SOCKET_FILE.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(SOCKET_FILE))
                    # Send both the current working directory and the file path
                    message = f"{os.getcwd()}||{args.file}"
                    client.sendall(message.encode())
                    print(f"Sent file path to existing instance: {args.file}")
                return
            except ConnectionRefusedError:
                print("No existing instance found, starting a new one.")
        else:
            print("No existing instance found, starting a new one.")

        app.command_line_file = args.file

    # Ensure the socket server is started before the app runs
    app.start_socket_server()

    app.run(sys.argv)

    if SOCKET_FILE.exists():
        SOCKET_FILE.unlink()


if __name__ == "__main__":
    main()

