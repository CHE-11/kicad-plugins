import pcbnew # type: ignore
import wx # type: ignore
import os
import sys
import logging

# Configure logging
log_file = os.path.join(os.path.dirname(__file__), "lcsc_importer.log")
logging.basicConfig(
    filename=log_file,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logging.info("LCSC Part Importer Plugin initialized")

# Check for dependencies and alert user if missing
def check_dependencies():
    missing_dependencies = []
    try:
        import pydantic # type: ignore
    except ImportError:
        missing_dependencies.append("pydantic")

    try:
        import requests # type: ignore
    except ImportError:
        missing_dependencies.append("requests")

    if missing_dependencies:
        missing_deps_str = ", ".join(missing_dependencies)
        message = (
            f"The following dependencies are missing: {missing_deps_str}.\n"
            "Please install them by running:\n"
            f"pip install {' '.join(missing_dependencies)}"
        )
        logging.warning(message)
        wx.MessageBox(message, "Missing Dependencies", style=wx.ICON_WARNING)
        raise ImportError(f"Missing dependencies: {missing_deps_str}")

# Run the dependency check
try:
    check_dependencies()
except ImportError as e:
    logging.error(f"Dependency check failed: {e}")
    raise e  # Stop execution if dependencies are missing

# Add the `easyeda2kicad` folder to sys.path to make it importable
easyeda_path = os.path.join(os.path.dirname(__file__), "easyeda2kicad")
sys.path.insert(0, easyeda_path)

try:
    from easyeda2kicad.__main__ import main  # import the main function from easyeda2kicad
    logging.info("Imported easyeda2kicad successfully")
except Exception as e:
    logging.error(f"Failed to import easyeda2kicad: {e}")
    wx.MessageBox(f"Error loading easyeda2kicad:\n{e}", "Import Error", style=wx.ICON_ERROR)
    raise e  # Stop execution if import fails

class EasyEDAImporterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "LCSC Part Importer"
        self.category = "Utility"
        self.description = "Import a part from LCSC to KiCad using easyeda2kicad"
        self.show_toolbar_button = True  # optional, defaults to False
        self.icon_file_name = os.path.join(easyeda_path, "lcsc_logo.png")
        logging.info("Plugin defaults set")

    def Run(self):
        logging.info("Run method started")
        # Open a dialog to get the LCSC part number
        dialog = wx.TextEntryDialog(None, "Enter LCSC Part Number:", "LCSC Part Importer", "")
        
        if dialog.ShowModal() == wx.ID_OK:
            part_number = dialog.GetValue().strip()
            logging.info(f"User entered part number: {part_number}")
            
            # Run easyeda2kicad's main function with the part number
            try:
                result = main(["--full", f"--lcsc_id={part_number}"])  # Pass arguments as needed
                logging.info(f"Part imported successfully: {result}")
                wx.MessageBox("Part imported successfully.", "Success")
            except Exception as e:
                error_message = f"Error importing part:\n{e}"
                logging.error(error_message)
                wx.MessageBox(error_message, "Execution Error", style=wx.ICON_ERROR)
        else:
            logging.info("Dialog was canceled by the user")
        
        dialog.Destroy()
        logging.info("Dialog closed")

# Register the plugin with KiCad
EasyEDAImporterPlugin().register()
logging.info("LCSC Part Importer Plugin registered")