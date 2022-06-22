# -*- coding: utf-8 -*-
"""
/***************************************************************************
 PdokServicesPlugin
                                 A QGIS plugin

                              -------------------
        begin                : 2012-10-11
        copyright            : (C) 2012 by Richard Duivenvoorde
        email                : richard@zuidt.nl
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
from copy import deepcopy
from optparse import check_choice
import re
from numpy import isin
from pytz import NonExistentTimeError
from qgis.PyQt.QtCore import (
    QSettings,
    QVariant,
    QFileInfo,
    Qt,
    QTranslator,
    QCoreApplication,
    qVersion,
    QTimer,
    QStringListModel,
    QModelIndex,
)
from qgis.PyQt.QtWidgets import (
    QAction,
    QLineEdit,
    QAbstractItemView,
    QMessageBox,
    QMenu,
    QToolButton,
    QCompleter,
)
from qgis.PyQt.QtGui import QIcon, QStandardItemModel, QStandardItem, QColor
from qgis.PyQt.QtCore import QSortFilterProxyModel, QRegExp
from qgis.core import (
    QgsApplication,
    Qgis,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsRectangle,
    QgsMessageLog,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsLayerTreeLayer,
)
from qgis.gui import QgsVertexMarker
import textwrap
import json
import os
import urllib.request, urllib.parse, urllib.error
import locale

# Initialize Qt resources from file resources.py
from . import resources_rc

# Import the code for the dialog
from .pdokservicesplugindialog import PdokServicesPluginDialog

from .processing_provider.provider import Provider

from .lib.http_client import PdokServicesNetworkException

from .locator_filter.pdoklocatieserverfilter import PDOKLocatieserverLocatorFilter

from .lib.constants import PLUGIN_NAME, PLUGIN_ID, DEFAULT_NR_FAVS, SETTINGS_SECTIONS
from .lib.locatieserver import (
    suggest_query,
    TypeFilter,
    LsType,
    lookup_object,
    get_lookup_object_url,
    Projection,
)


class PdokServicesPlugin(object):
    def __init__(self, iface):
        # Save reference to the QGIS interface
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        # services dialog
        self.dlg = PdokServicesPluginDialog(parent=self.iface.mainWindow())

        # locator filter
        self.filter = PDOKLocatieserverLocatorFilter(self.iface)
        self.iface.registerLocatorFilter(self.filter)

        # initialize plugin directory
        self.currentLayer = None
        self.SETTINGS_SECTION = SETTINGS_SECTIONS
        self.pointer = None
        self.geocoder_source_model = None

        self.fq_checkboxes = {
            self.dlg.ui.cbx_gem: LsType.gemeente,
            self.dlg.ui.cbx_wpl: LsType.woonplaats,
            self.dlg.ui.cbx_weg: LsType.weg,
            self.dlg.ui.cbx_pcd: LsType.postcode,
            self.dlg.ui.cbx_adr: LsType.adres,
            self.dlg.ui.cbx_pcl: LsType.perceel,
            self.dlg.ui.cbx_hmp: LsType.hectometerpaal,
        }
        self.fav_actions = []

        self.provider = Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)


    def get_settings_value(self, key, default=""):
        if QSettings().contains(f"{self.SETTINGS_SECTION}{key}"):
            key = f"{self.SETTINGS_SECTION}{key}"
            if Qgis.QGIS_VERSION_INT < 10900:  # qgis <= 1.8
                return str(QSettings().value(key).toString())
            else:
                return str(QSettings().value(key))
        else:
            return default

    def set_settings_value(self, key, value):
        key = f"{self.SETTINGS_SECTION}{key}"
        if Qgis.QGIS_VERSION_INT < 10900:
            # qgis <= 1.8
            QSettings().setValue(key, QVariant(value))
        else:
            QSettings().setValue(key, value)

    def add_fav_actions_to_toolbar_button(self, nr_of_favs):
        # first reset existing fav_actions
        for fav_action in self.fav_actions:
            self.run_button.menu().removeAction(fav_action)
        self.fav_actions = []

        # add fav_actions
        for i in range(1, nr_of_favs + 1):
            fav_action = QAction(f"Favoriet {i}", self.iface.mainWindow())
            fav_action.setIcon(self.runIcon)
            fav_action.triggered.connect(
                (lambda i: lambda: self.load_favourite(i))(i)
            )  # Not sure if lambda is required, double lambda is required in order to freeze argument, otherwise always last favourite is added
            # see https://stackoverflow.com/a/10452866/1763690
            self.set_favourite_action(fav_action, i)
            self.run_button.menu().addAction(fav_action)
            self.fav_actions.append(fav_action)

    def initGui(self):
        """Create action that will start plugin configuration

        Function name should be kept as is, since it is required for a QGIS plugin. So does not conform with pep naming convention.
        """
        self.runIcon = QIcon(
            os.path.join(self.plugin_dir, "resources", "icon_add_service.svg")
        )

        self.run_action = QAction(self.runIcon, PLUGIN_NAME, self.iface.mainWindow())
        self.run_button = QToolButton()
        self.run_button.setMenu(QMenu())
        self.run_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.run_button.setDefaultAction(self.run_action)

        self.services_loaded = False
        # connect the action to the run method
        self.run_action.triggered.connect(self.run)
        self.setup_fq_checkboxes()

        # Add toolbar button and menu item
        self.toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self.toolbar.setObjectName(PLUGIN_NAME)
        self.toolbar.addWidget(self.run_button)

        # Set default loading behaviour
        self.default_tree_locations = {
            "wms": "top",
            "wmts": "bottom",
            "wfs": "top",
            "wcs": "top",
        }

        def update_nr_of_favs():
            nr_of_favs = self.dlg.ui.nr_favs_input.value()
            QSettings().setValue(f"/{PLUGIN_ID}/nr_of_favs", nr_of_favs)
            self.add_fav_actions_to_toolbar_button(nr_of_favs)

        self.dlg.ui.nr_favs_input.valueChanged.connect(update_nr_of_favs)
        nr_of_favs = int(QSettings().value(f"/{PLUGIN_ID}/nr_of_favs", DEFAULT_NR_FAVS))
        self.dlg.ui.nr_favs_input.setValue(nr_of_favs)
        self.add_fav_actions_to_toolbar_button(nr_of_favs)

        self.toolbar_search = QLineEdit()
        self.toolbar_search.setMaximumWidth(200)
        self.toolbar_search.setAlignment(Qt.AlignLeft)
        self.toolbar_search.setPlaceholderText("Zoek in PDOK Locatieserver")
        self.toolbar.addWidget(self.toolbar_search)
        self.timer_toolbar_search = QTimer()
        self.timer_toolbar_search.setSingleShot(True)
        self.timer_toolbar_search.setInterval(200)
        self.timer_toolbar_search.timeout.connect(self.toolbar_search_get_suggestions)
        self.toolbar_search.textEdited.connect(
            lambda: self.timer_toolbar_search.start()
        )

        # address/point cleanup
        eraser_icon = QIcon(
            os.path.join(self.plugin_dir, "resources", "icon_remove_cross.svg")
        )
        self.clean_action = QAction(eraser_icon, "Cleanup", self.erase_address())
        self.toolbar.addAction(self.clean_action)
        self.clean_action.triggered.connect(self.erase_address)
        self.clean_action.setEnabled(False)
        self.iface.addPluginToMenu(f"&{PLUGIN_NAME}", self.run_action)

        # about
        self.about_action = QAction(self.runIcon, "About", self.iface.mainWindow())
        self.about_action.setWhatsThis(f"{PLUGIN_NAME} About")
        self.iface.addPluginToMenu(f"&{PLUGIN_NAME}", self.about_action)

        self.about_action.triggered.connect(self.about)
        self.dlg.ui.btnLoadLayer.clicked.connect(lambda: self.load_layer("default"))
        self.dlg.ui.btnLoadLayerTop.clicked.connect(lambda: self.load_layer("top"))
        self.dlg.ui.btnLoadLayerBottom.clicked.connect(
            lambda: self.load_layer("bottom")
        )

        self.dlg.ui.pushButton.clicked.connect(self.toggle_all_fq_checkboxes)

        self.dlg.geocoder_search.returnPressed.connect(
            self.ls_dialog_get_suggestions_and_remove_pointer
        )
        self.timer_geocoder_search = QTimer()
        self.timer_geocoder_search.setSingleShot(True)
        self.timer_geocoder_search.setInterval(200)
        self.timer_geocoder_search.timeout.connect(
            self.ls_dialog_get_suggestions_and_remove_pointer
        )
        self.dlg.geocoder_search.textEdited.connect(
            lambda: self.timer_geocoder_search.start()
        )

        self.dlg.geocoder_search.setPlaceholderText(
            "Zoek in PDOK Locatieserver, bv postcode of postcode huisnummer"
        )

        self.dlg.geocoderResultSearch.textChanged.connect(self.filter_geocoder_result)
        self.dlg.geocoderResultSearch.setPlaceholderText(
            "een of meer zoekwoorden uit resultaat"
        )
        # connect all fq checkboxes with suggest, so upon a change in fq filter we re-search
        for cbx in self.fq_checkboxes.keys():
            cbx.stateChanged.connect(self.ls_dialog_get_suggestions_and_remove_pointer)
        self.run(True)

        # set to hidden when no layer selected
        self.dlg.ui.layer_info.setHidden(True)
        self.dlg.ui.layer_options_groupbox.setHidden(True)

    def about(self):
        infoString = textwrap.dedent(
            """
            Written by Richard Duivenvoorde
            Email - richard@duif.net
            Company - Zuidt - https://www.zuidt.nl
            Source: https://github.com/rduivenvoorde/pdokservicesplugin
            """
        )
        QMessageBox.information(
            self.iface.mainWindow(), f"{PLUGIN_NAME} - About", infoString
        )

    def unload(self):
        try:  # using try except here because plugin could be unloaded during development: gracefully fail
            self.remove_pointer()
            self.iface.removePluginMenu(f"&{PLUGIN_NAME}", self.run_action)
            self.iface.removePluginMenu(f"&{PLUGIN_NAME}", self.about_action)
            del self.toolbar
        except Exception as e:
            pass
        QgsApplication.processingRegistry().removeProvider(self.provider)

    def get_dd(self, val, val_string=""):
        md_item_empty = "<dd><em>Niet ingevuld</em></dd>"
        if val:
            if val_string:
                val = val_string
            return f"<dd>{val}</dd>"
        return md_item_empty

    def format_scale_denominator(self, val):
        if val == "" or not val.isnumeric():
            return ""
        locale.setlocale(
            locale.LC_ALL, "nl_NL"
        )  # enforce dutch locale, to ensure 1000 seperators is "."
        scale_formatted = locale.format_string("%d", int(float(val)), grouping=True)
        return f"1:{scale_formatted}"

    def show_layer(self, selectedIndexes):
        if len(selectedIndexes) == 0:
            self.currentLayer = None
            self.dlg.ui.layer_info.setHtml("")
            self.dlg.ui.comboSelectProj.clear()
            self.dlg.ui.layer_info.setHidden(True)
            self.dlg.ui.layer_options_groupbox.setHidden(True)
            return

        self.dlg.ui.layer_info.setHidden(False)
        self.dlg.ui.layer_options_groupbox.setHidden(False)

        # needed to scroll To the selected row incase of using the keyboard / arrows
        self.dlg.servicesView.scrollTo(self.dlg.servicesView.selectedIndexes()[0])
        # itemType holds the data (== column 1)
        self.currentLayer = self.dlg.servicesView.selectedIndexes()[1].data(Qt.UserRole)
        if isinstance(self.currentLayer, QVariant):
            self.currentLayer = self.currentLayer.toMap()
            # QGIS 1.8: QVariants
            currentLayer = {}
            for key in list(self.currentLayer.keys()):
                val = self.currentLayer[key]
                currentLayer[str(key)] = str(val.toString())
            self.currentLayer = currentLayer
        url = self.currentLayer["service_url"]
        title = self.currentLayer["title"]
        abstract_dd = self.get_dd(self.currentLayer["abstract"])

        service_title = (
            self.currentLayer["service_title"]
            if self.currentLayer["service_title"]
            else "[service title niet ingevuld]"
        )
        layername = self.currentLayer["name"]
        service_abstract_dd = self.get_dd(self.currentLayer["service_abstract"])
        stype = self.currentLayer["service_type"].upper()
        minscale = ""
        if "minscale" in self.currentLayer:
            minscale = self.format_scale_denominator(self.currentLayer["minscale"])
        maxscale = ""
        if "maxscale" in self.currentLayer:
            maxscale = self.format_scale_denominator(self.currentLayer["maxscale"])
        service_md_id = self.currentLayer["service_md_id"]
        dataset_md_id = self.currentLayer["dataset_md_id"]
        self.dlg.ui.layer_info.setText("")
        self.dlg.ui.btnLoadLayer.setEnabled(True)
        self.dlg.ui.btnLoadLayerTop.setEnabled(True)
        self.dlg.ui.btnLoadLayerBottom.setEnabled(True)

        maxscale_string = ""
        if maxscale:
            maxscale_string = f"""
            <dt><b>Maxscale</b></dt>
            <dd>{maxscale}</a></dd>
            """
        minscale_string = ""
        if minscale:
            minscale_string = f"""
            <dt><b>Minscale</b></dt>
            <dd>{minscale}</a></dd>
            """

        layername_key_mapping = {
            "WCS": "Coverage",
            "WMS": "Layer",
            "WMTS": "Layer",
            "WFS": "Featuretype",
        }
        layername_key = f"{layername_key_mapping[stype]}"
        dataset_metadata_dd = self.get_dd(
            dataset_md_id,
            f'<a href="https://www.nationaalgeoregister.nl/geonetwork/srv/dut/catalog.search#/metadata/{dataset_md_id}">{dataset_md_id}</a>',
        )

        self.dlg.ui.layer_info.setHtml(
            f"""
            <h2><a href="{url}">{service_title} - {stype}</a></h2>
            <dl>
                <dt><b>Service Abstract</b></dt>
                {service_abstract_dd}
                <!--<dt><b>Service Url</b></dt>
                <dd>{url}</dd>-->
                <dt><b>Service Metadata</b></dt>
                <dd><a href="https://www.nationaalgeoregister.nl/geonetwork/srv/dut/catalog.search#/metadata/{service_md_id}">{service_md_id}</a></dd>
            </dl>
            <h3>{layername_key}: {title}</h3>
            <dl>
                <dt><b>Name</b></dt>
                <dd>{layername}</a></dd>
                <dt><b>Abstract</b></dt>
                {abstract_dd}
                <dt><b>Dataset Metadata</b></dt>
                {dataset_metadata_dd}
                {minscale_string}
                {maxscale_string}
            </dl>
            """
        )
        self.dlg.ui.comboSelectProj.clear()
        self.dlg.ui.wmsStyleComboBox.clear()

        show_list = {
            self.dlg.ui.comboSelectProj: ["WMS", "WMTS"],
            self.dlg.ui.labelCrs: ["WMS", "WMTS"],
            self.dlg.ui.wmsStyleComboBox: ["WMS"],
            self.dlg.ui.wmsStyleLabel: ["WMS"],
        }

        for ui_el in show_list.keys():
            service_types = show_list[ui_el]
            ui_el.setHidden(not (stype in service_types))

        if stype == "WMS":
            styles = self.currentLayer["styles"]
            nr_styles = len(styles)
            style_str = "styles" if nr_styles > 1 else "style"
            self.dlg.ui.wmsStyleLabel.setText(
                f"Style ({nr_styles} {style_str} beschikbaar)"
            )
            style_title_names = [
                x["title"] if "title" in x else x["name"] for x in styles
            ]
            self.dlg.ui.wmsStyleComboBox.addItems(style_title_names)
            self.dlg.ui.wmsStyleComboBox.setCurrentIndex(0)
            completer = QCompleter(style_title_names, self.dlg.ui.wmsStyleComboBox)
            completer.setFilterMode(Qt.MatchContains)
            self.dlg.ui.wmsStyleComboBox.setCompleter(completer)
            self.dlg.ui.wmsStyleComboBox.setEnabled(
                nr_styles > 1  # enable if more than one style
            )
            try:
                crs = self.currentLayer["crs"]
            except KeyError:
                crs = "EPSG:28992"
            crs = crs.split(",")
            self.dlg.ui.comboSelectProj.addItems(crs)
            for i in range(len(crs)):
                if crs[i] == "EPSG:28992":
                    self.dlg.ui.comboSelectProj.setCurrentIndex(i)

        if stype == "WMTS":
            tilematrixsets = self.currentLayer["tilematrixsets"].split(",")
            self.dlg.ui.comboSelectProj.addItems(tilematrixsets)
            for i in range(len(tilematrixsets)):
                if tilematrixsets[i].startswith("EPSG:28992"):
                    self.dlg.ui.comboSelectProj.setCurrentIndex(i)

    def set_favourite_action(self, action, favourite_number):
        if QSettings().contains(f"/{PLUGIN_ID}/favourite_{favourite_number}"):
            layer = QSettings().value(
                f"/{PLUGIN_ID}/favourite_{favourite_number}", None
            )

            if layer:
                action.setToolTip(layer["title"].capitalize())
                title = layer["title"].capitalize()
                if "selectedStyle" in layer:
                    style = layer["selectedStyle"]
                    style_title = style["name"]
                    if "title" in style:
                        style_title = style["title"]
                    if style_title:
                        title = f"{title} [{style_title}]"

                if "service_type" in layer:
                    stype = layer["service_type"].upper()
                    title += f" ({stype})"
                action.setText(title)
                action.setIcon(self.runIcon)

    def get_layer_in_pdok_layers(self, lyr):
        """check for layer equality based on equal
        - service_md_id
        - name (layername)
        - style (in case of WMS layer)
        returns None if layer not found
        """

        def predicate(x):
            if x["service_md_id"] == lyr["service_md_id"] and x["name"] == lyr["name"]:
                # WMS layer with style
                if "style" in x and "style" in lyr:
                    if x["style"] == lyr["style"]:
                        return True
                    else:
                        return False
                # other layer without style (but with matching layername and service_md_id)
                return True
            return False

        return next(filter(predicate, self.layers_pdok), None)

    def load_favourite(self, favourite_number):
        if QSettings().contains(f"/{PLUGIN_ID}/favourite_{favourite_number}"):
            saved_layer = QSettings().value(
                f"/{PLUGIN_ID}/favourite_{favourite_number}", None
            )
            # migration code required for change: https://github.com/rduivenvoorde/pdokservicesplugin/commit/a5700dace54250b8f18229939907c3cab39f5297
            # which changed the schema of the layer config json file
            migrate_fav = False
            if "md_id" in saved_layer:
                saved_layer["service_md_id"] = saved_layer["md_id"]
                migrate_fav = True
            if "layers" in saved_layer:
                saved_layer["name"] = saved_layer["layers"]
                migrate_fav = True
            layer = self.get_layer_in_pdok_layers(saved_layer)
            if migrate_fav:
                QSettings().setValue(
                    f"/{PLUGIN_ID}/favourite_{favourite_number}", layer
                )
            if layer:
                self.currentLayer = layer
                self.load_layer()
                return
        self.show_warning(
            "Maak een Favoriet aan door in de dialoog met services en lagen via het context menu (rechter muisknop) een Favoriet te kiezen...",
            "Geen Favoriet aanwezig (of verouderd)...",
        )
        self.run()

    def quote_wmts_url(self, url):
        """
        Quoten wmts url is nodig omdat qgis de query param `SERVICE=WMS` erachter plakt als je de wmts url niet quote.
        Dit vermoedelijk omdat de wmts laag wordt toegevoegd mbv de wms provider: `return QgsRasterLayer(uri, title, "wms")`.
        Wat op basis van de documentatie wel de manier is om een wmts laag toe te voegen.
        """
        parse_result = urllib.parse.urlparse(url)
        location = f"{parse_result.scheme}://{parse_result.netloc}/{parse_result.path}"
        query = parse_result.query
        query_escaped_quoted = urllib.parse.quote_plus(query)
        url = f"{location}?{query_escaped_quoted}"
        return url

    def get_selected_style(self):
        selected_style_title = self.dlg.ui.wmsStyleComboBox.currentText()
        selected_style = None
        if "styles" in self.currentLayer:
            selected_style = next(
                (
                    x
                    for x in self.currentLayer["styles"]
                    if x["title"] == selected_style_title
                ),
                None,
            )
            if selected_style is None:
                # check if selected_style_title is one of the style names, in case the style in the cap doc does not have a title
                # style should have at least a name
                selected_style = next(
                    (
                        x
                        for x in self.currentLayer["styles"]
                        if x["name"] == selected_style_title
                    ),
                    None,
                )
        return selected_style

    def create_new_layer(self):
        servicetype = self.currentLayer["service_type"]
        title = self.currentLayer["title"]
        layername = self.currentLayer["name"]
        url = self.currentLayer["service_url"]

        if servicetype == "wms":
            imgformat = self.currentLayer["imgformats"].split(",")[0]
            if self.dlg.ui.comboSelectProj.currentIndex() == -1:
                crs = "EPSG:28992"
            else:
                crs = self.dlg.ui.comboSelectProj.currentText()
            if Qgis.QGIS_VERSION_INT < 10900:
                # qgis <= 1.8
                uri = url
                self.iface.addRasterLayer(
                    uri,
                    title,
                    "wms",
                    [layername],
                    [""],
                    imgformat,
                    crs,
                )
            else:
                # qgis > 1.8
                selected_style_name = ""
                selected_style = self.get_selected_style()
                if selected_style is not None:
                    selected_style_name = selected_style["name"]
                    selected_style_title = selected_style["name"]
                    if "title" in selected_style:
                        selected_style_title = selected_style["title"]
                    title += f" [{selected_style_title}]"

                uri = f"crs={crs}&layers={layername}&styles={selected_style_name}&format={imgformat}&url={url}"
                return QgsRasterLayer(uri, title, "wms")
        elif servicetype == "wmts":
            if Qgis.QGIS_VERSION_INT < 10900:
                self.show_warning(
                    f"""Sorry, dit type layer: '{servicetype.upper()}'
                    kan niet worden geladen in deze versie van QGIS.
                    Misschien kunt u QGIS 2.0 installeren (die kan het WEL)?
                    Of is de laag niet ook beschikbaar als wms of wfs?"""
                )
                return None
            url = self.quote_wmts_url(url)
            if self.dlg.ui.comboSelectProj.currentIndex() == -1:
                tilematrixset = "EPSG:28992"
            else:
                tilematrixset = self.dlg.ui.comboSelectProj.currentText()
            imgformat = self.currentLayer["imgformats"].split(",")[0]
            if tilematrixset.startswith("EPSG:"):
                crs = tilematrixset
                i = crs.find(":", 5)
                if i > -1:
                    crs = crs[:i]
            elif tilematrixset.startswith("OGC:1.0"):
                crs = "EPSG:3857"
            uri = f"tileMatrixSet={tilematrixset}&crs={crs}&layers={layername}&styles=default&format={imgformat}&url={url}"
            return QgsRasterLayer(
                uri, title, "wms"
            )  # `wms` is correct, zie ook quote_wmts_url
        elif servicetype == "wfs":
            uri = f" pagingEnabled='true' restrictToRequestBBOX='1' srsname='EPSG:28992' typename='{layername}' url='{url}' version='2.0.0'"
            return QgsVectorLayer(uri, title, "wfs")
        elif servicetype == "wcs":
            format = "GEOTIFF_FLOAT32"
            # we handcrafted some wcs layers with 2 different image formats: tiff (RGB) and tiff (float32):
            if "imgformats" in self.currentLayer:
                format = self.currentLayer["imgformats"].split(",")[0]
            uri = f"cache=AlwaysNetwork&crs=EPSG:28992&format={format}&identifier={layername}&url={url}"
            return QgsRasterLayer(uri, title, "wcs")
        else:
            self.show_warning(
                f"""Sorry, dit type laag: '{servicetype.upper()}'
                kan niet worden geladen door de plugin of door QGIS.
                Is het niet beschikbaar als wms, wmts of wfs?
                """
            )
            return

    def load_layer(self, tree_location=None):
        if self.currentLayer == None:
            return
        servicetype = self.currentLayer["service_type"]
        if tree_location is None:
            tree_location = self.default_tree_locations[servicetype]
        new_layer = self.create_new_layer()
        if new_layer is None:
            return
        self.add_layer(new_layer, tree_location)

    def add_layer(self, new_layer, tree_location="default"):
        """Adds a QgsLayer to the project and layer tree.
        tree_location can be 'default', 'top', 'bottom'
        """
        if tree_location not in ["default", "top", "bottom"]:
            # TODO: proper error handling
            return
        if tree_location == "default":
            QgsProject.instance().addMapLayer(new_layer, True)
            return
        QgsProject.instance().addMapLayer(new_layer, False)
        new_layer_tree_layer = QgsLayerTreeLayer(new_layer)
        layer_tree = self.iface.layerTreeCanvasBridge().rootGroup()
        if tree_location == "top":
            layer_tree.insertChildNode(0, new_layer_tree_layer)
        if tree_location == "bottom":
            layer_tree.insertChildNode(-1, new_layer_tree_layer)

    def filter_geocoder_result(self, string):
        self.dlg.geocoderResultView.selectRow(0)
        self.geocoderProxyModel.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.geocoderProxyModel.setFilterFixedString(string)

    def toolbar_search_get_suggestions(self):
        def create_model(_suggestions):
            model = QStandardItemModel()
            for s in _suggestions:
                key = s["weergavenaam"]
                it = QStandardItem(key)
                it.setData(s, Qt.UserRole)
                model.appendRow(it)
            return model

        search_text = self.toolbar_search.text()
        if len(search_text) <= 1:
            self.toolbar_search.setCompleter(None)
            return
        results = suggest_query(search_text, self.create_type_filter())
        self.completer = QCompleter()
        self.model = create_model(results)
        self.completer.setModel(self.model)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setFilterMode(Qt.MatchContains)
        self.toolbar_search.setCompleter(self.completer)
        self.toolbar_search.show()
        self.completer.complete()
        self.completer.activated.connect(self.on_toolbar_suggest_activated)
        return

    def on_toolbar_suggest_activated(self, suggest_text):
        items = self.model.findItems(suggest_text)
        if len(items) == 0:  # check should not be necessary
            return
        item = items[0]
        data = item.data(Qt.UserRole)
        lookup_id = data["id"]
        self.lookup_toolbar_search_and_zoom(lookup_id)
        self.dlg.geocoder_search.setText(suggest_text)
        self.fill_ls_dialog_from_toolbar_search()  # run geocode to populate ls dialog

    def ls_dialog_get_suggestions_and_remove_pointer(self):
        self.remove_pointer()
        self.geocoder_source_model.clear()
        self.ls_dialog_get_suggestions()

    def ls_dialog_get_suggestions(self):
        try:
            self.dlg.ui.lookupinfo.setHtml("")
            search_text = self.dlg.geocoder_search.text()
            if len(search_text) <= 1:
                return
            results = suggest_query(search_text, self.create_type_filter(), 50)
            if len(results) == 0:
                # ignore, as we are suggesting, maybe more characters will reveal something...
                return
            for result in results:
                adrestekst = QStandardItem(str(result["weergavenaam"]))
                adrestekst.setData(result, Qt.UserRole)
                type = QStandardItem(str(result["type"]))
                adrestekst.setData(result, Qt.UserRole)
                self.geocoder_source_model.appendRow([adrestekst, type])
            self.geocoder_source_model.setHeaderData(0, Qt.Horizontal, "Resultaat")
            self.geocoder_source_model.setHeaderData(1, Qt.Horizontal, "Type")
            self.geocoder_source_model.horizontalHeaderItem(0).setTextAlignment(
                Qt.AlignLeft
            )
            self.dlg.geocoderResultView.resizeColumnsToContents()
            self.dlg.geocoderResultView.horizontalHeader().setStretchLastSection(True)
        except PdokServicesNetworkException as ex:
            title = f"{PLUGIN_NAME} - HTTP Request Error"
            message = f"""an error occured while executing HTTP request, error:
                    {str(ex)}
                    """
            self.show_error(message, title)

    def erase_address(self):
        """
        clean the input and remove the pointer
        """
        self.remove_pointer()
        if self.geocoder_source_model is not None:
            self.geocoder_source_model.clear()
        if self.dlg.geocoder_search is not None:
            self.dlg.geocoder_search.clear()
        if self.toolbar_search is not None:
            self.toolbar_search.clear()
            self.toolbar_search.setCompleter(None)

    def filter_layers(self, string):
        # remove selection if one row is selected
        self.dlg.servicesView.selectRow(0)
        self.proxyModel.setFilterCaseSensitivity(Qt.CaseInsensitive)
        strlist = string.strip().split(" ")
        string = ""
        for s in strlist:
            string += f"{s}.*"
        self.info(f"zoektekst: {string}")
        regexp = QRegExp(string, Qt.CaseInsensitive)
        regexp.setMinimal(True)
        self.proxyModel.setFilterRegExp(regexp)

    def add_source_row(self, serviceLayer):
        # you can attache different "data's" to to an QStandarditem
        # default one is the visible one:
        itemType = QStandardItem(str(serviceLayer["service_type"].upper()))
        # userrole is a free form one:
        # only attach the data to the first item
        # service layer = a dict/object with all props of the layer
        itemType.setData(serviceLayer, Qt.UserRole)
        itemType.setToolTip(
            f'{serviceLayer["service_type"].upper()} - {serviceLayer["title"]}'
        )
        # only wms services have styles (sometimes)
        layername = serviceLayer["title"]
        styles_string = ""
        if "styles" in serviceLayer:
            styles_string = " ".join(
                [" ".join(x.values()) for x in serviceLayer["styles"]]
            )

        itemLayername = QStandardItem(str(serviceLayer["title"]))
        itemLayername.setToolTip(
            f'{serviceLayer["service_type"].upper()} - {serviceLayer["service_title"]}'
        )
        # itemFilter is the item used to search filter in. That is why layername is a combi of layername + filter here
        itemFilter = QStandardItem(
            f'{serviceLayer["service_type"]} {layername} {serviceLayer["service_title"]} {serviceLayer["service_abstract"]} {styles_string}'
        )
        itemServicetitle = QStandardItem(str(serviceLayer["service_title"]))
        itemServicetitle.setToolTip(
            f'{serviceLayer["service_type"].upper()} - {serviceLayer["title"]}'
        )
        self.sourceModel.appendRow(
            [itemLayername, itemType, itemServicetitle, itemFilter]
        )

    def run(self, hiddenDialog=False):
        """
        run method that performs all the real work
        """
        # enable possible remote pycharm debugging
        # import pydevd
        # pydevd.settrace('localhost', port=5678, stdoutToServer=True, stderrToServer=True)

        # last viewed/selected tab
        if QSettings().contains(f"/{PLUGIN_ID}/currenttab"):
            if Qgis.QGIS_VERSION_INT < 10900:
                # qgis <= 1.8
                self.dlg.tabs.widget(
                    QSettings().value(f"/{PLUGIN_ID}/currenttab").toInt()[0]
                )
            else:
                self.dlg.tabs.widget(int(QSettings().value(f"/{PLUGIN_ID}/currenttab")))

        if self.services_loaded == False:
            pdokjson = os.path.join(self.plugin_dir, "resources", "layers-pdok.json")
            with open(pdokjson, "r", encoding="utf-8") as f:
                self.layers_pdok = json.load(f)

            self.sourceModel = QStandardItemModel()

            self.styleFilter = QSortFilterProxyModel()
            self.styleFilter.setSourceModel(self.sourceModel)
            self.styleFilter.setFilterKeyColumn(4)

            self.proxyModel = QSortFilterProxyModel()
            self.proxyModel.setSourceModel(self.styleFilter)
            self.proxyModel.setFilterKeyColumn(3)

            self.dlg.servicesView.setModel(self.proxyModel)
            self.dlg.servicesView.setEditTriggers(QAbstractItemView.NoEditTriggers)

            self.geocoderProxyModel = QSortFilterProxyModel()
            self.geocoder_source_model = QStandardItemModel()

            self.geocoderProxyModel.setSourceModel(self.geocoder_source_model)
            self.geocoderProxyModel.setFilterKeyColumn(0)
            self.dlg.geocoderResultView.setModel(self.geocoderProxyModel)
            self.dlg.geocoderResultView.setEditTriggers(
                QAbstractItemView.NoEditTriggers
            )
            for layer in self.layers_pdok:
                if isinstance(layer["name"], str):
                    self.add_source_row(layer)

            self.dlg.layerSearch.textChanged.connect(self.filter_layers)
            self.dlg.servicesView.selectionModel().selectionChanged.connect(
                self.show_layer
            )
            self.dlg.servicesView.doubleClicked.connect(
                lambda: self.load_layer(None)
            )  # Using lambda here to prevent sending signal parameters to the loadService() function

            self.dlg.servicesView.setContextMenuPolicy(Qt.CustomContextMenu)
            self.dlg.servicesView.customContextMenuRequested.connect(
                self.make_favourite
            )

            # actually I want to load a service when doubleclicked on header
            # but as I cannot get this to work, let's disable clicking it then
            self.dlg.servicesView.verticalHeader().setSectionsClickable(False)
            self.dlg.servicesView.horizontalHeader().setSectionsClickable(False)
            self.dlg.geocoderResultView.selectionModel().selectionChanged.connect(
                self.lookup_dialog_search
            )
            # hide itemFilter column:
            self.dlg.servicesView.hideColumn(3)
            self.services_loaded = True

        self.sourceModel.setHeaderData(2, Qt.Horizontal, "Service")
        self.sourceModel.setHeaderData(1, Qt.Horizontal, "Type")
        self.sourceModel.setHeaderData(0, Qt.Horizontal, "Laagnaam")
        self.sourceModel.horizontalHeaderItem(2).setTextAlignment(Qt.AlignLeft)
        self.sourceModel.horizontalHeaderItem(1).setTextAlignment(Qt.AlignLeft)
        self.sourceModel.horizontalHeaderItem(0).setTextAlignment(Qt.AlignLeft)
        self.dlg.servicesView.setColumnWidth(
            0, 300
        )  # set name to 300px (there are some huge layernames)
        self.dlg.servicesView.horizontalHeader().setStretchLastSection(True)
        # show the dialog ?
        if not hiddenDialog:
            self.dlg.show()
        # Run the dialog event loop
        # result = self.dlg.exec_()
        if Qgis.QGIS_VERSION_INT < 10900:
            # qgis <= 1.8
            QSettings().setValue(
                f"/{PLUGIN_ID}/currenttab", QVariant(self.dlg.tabs.currentIndex())
            )
        else:
            QSettings().setValue(
                f"/{PLUGIN_ID}/currenttab", self.dlg.tabs.currentIndex()
            )
        self.remove_pointer()

    def make_favourite(self, position):
        menu = QMenu()
        nr_of_favs = int(QSettings().value(f"/{PLUGIN_ID}/nr_of_favs", DEFAULT_NR_FAVS))
        actions = [
            menu.addAction(f"Maak Deze Laag Favoriet {x}")
            for x in range(1, nr_of_favs + 1)
        ]
        action = menu.exec_(self.dlg.servicesView.mapToGlobal(position))
        if action is not None:
            index = actions.index(action)
            if index != -1:
                current_layer = self.currentLayer
                selected_style = self.get_selected_style()
                if selected_style is not None:
                    current_layer = {
                        **self.currentLayer,
                        **{"selectedStyle": selected_style},
                    }
                QSettings().setValue(
                    f"/{PLUGIN_ID}/favourite_{index+1}", current_layer
                )  # index +1 since favourite settings indexes are 1 based...
                self.set_favourite_action(self.fav_actions[index], index + 1)

    def setup_fq_checkboxes(self):
        """
        Setup the fq checkboxes in the gui, by looking into the settings for the
        'pdokservicesplugin/checkedfqs' key, which contains a list of type strings
        like ['weg','adres']
        """
        checked_fqs = self.get_settings_value("checkedfqs", [])
        if len(checked_fqs) > 0:  # else there is not saved state... take gui defaults
            for checkbox in self.fq_checkboxes.keys():
                ls_type = self.fq_checkboxes[checkbox]
                checkbox.setChecked(ls_type.name in checked_fqs)

    def toggle_all_fq_checkboxes(self):
        none_checked = all(map(lambda x: not x.isChecked(), self.fq_checkboxes.keys()))
        if none_checked:
            # check_all
            [x.setChecked(True) for x in self.fq_checkboxes.keys()]
        else:
            # uncheck all
            [x.setChecked(False) for x in self.fq_checkboxes.keys()]

    def create_type_filter(self):
        """
        This creates a TypeFilter (Filter Query, see https://github.com/PDOK/locatieserver/wiki/Zoekvoorbeelden-Locatieserver) based on the checkboxes in the dialog. Defaults to []
        """
        filter = TypeFilter([])
        for key in self.fq_checkboxes.keys():
            if key.isChecked():
                filter.add_type(self.fq_checkboxes[key])
        return filter

    def fill_ls_dialog_from_toolbar_search(self):
        self.dlg.geocoder_search.setText(self.toolbar_search.text())
        self.geocoder_source_model.clear() # otherwise results will be appended in in ls_dialog
        self.ls_dialog_get_suggestions()

    def lookup_toolbar_search_and_zoom(self, lookup_id):
        data = None
        try:
            data = lookup_object(lookup_id, Projection.EPSG_28992)
        except PdokServicesNetworkException as ex:
            title = f"{PLUGIN_NAME} - HTTP Request Error"
            message = textwrap.dedent(
                f"""an error occured while executing HTTP request, error:

                {str(ex)}
                """
            )
            self.show_error(message, title)
        if data is None:
            return
        self.zoom_to_result(data)

    def zoom_to_result(self, data):
        # just always transform from 28992 to mapcanvas crs
        crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        crs28992 = QgsCoordinateReferenceSystem.fromEpsgId(28992)
        crsTransform = QgsCoordinateTransform(crs28992, crs, QgsProject.instance())

        adrestekst = "{} - {}".format(data["type"], data["weergavenaam"])
        adrestekst_lower = adrestekst.lower()

        zoom_dict = {
            "adres": 794,
            "perceel": 794,
            "hectometer": 1587,
            "weg": 3175,
            "postcode": 6350,
            "woonplaats": 25398,
            "gemeente": 50797,
            "provincie": 812750,
        }
        z = 1587
        for z_type in zoom_dict.keys():
            if adrestekst_lower.startswith(
                z_type
            ):  # maybe find better way to infer return type?
                z = zoom_dict[z_type]

        centroid = QgsGeometry.fromWkt(data["wkt_centroid"])
        centroid.transform(crsTransform)
        center = centroid.asPoint()
        self.set_pointer(center)
        geom = QgsGeometry.fromWkt(data["wkt_geom"])
        geom.transform(crsTransform)
        geom_bbox = geom.boundingBox()
        rect = QgsRectangle(geom_bbox)
        self.iface.mapCanvas().zoomToFeatureExtent(rect)
        # zoom to a point feature is actually setting a point rectangle and then zoom
        if re.match(r"^POINT", data["wkt_geom"]):
            self.iface.mapCanvas().zoomScale(z)
        self.iface.mapCanvas().refresh()

    def fill_lookup_info(self, data):
        lookup_url = get_lookup_object_url(data["id"])
        data["lookup_url"] = lookup_url
        # generate lookupinfo list
        data_sorted = {}
        # lambda function to ensure values starting with _ are place last
        # see https://stackoverflow.com/a/18875168/1763690
        for key in sorted(data.keys(), key=lambda d: d.lower().replace("_", "{")):
            data_sorted[key] = data[key]
        result_list = ""
        for key in data_sorted.keys():
            if key in ["wkt_centroid", "wkt_geom"]:  # skip geom fields
                continue
            val = data_sorted[key]
            if isinstance(val, str) and re.match(r"^https?:\/\/.*$", val):
                val = f'<a href="{val}">{val}</a>'
            if isinstance(val, list):
                val = ", ".join(val)
            result_list = f"{result_list}<li><b>{key}:</b> {val}</li>"
        self.dlg.ui.lookupinfo.setHtml(f"<lu>{result_list}</lu>")

    def lookup_dialog_search(self):
        self.remove_pointer()
        data = self.dlg.geocoderResultView.selectedIndexes()[0].data(Qt.UserRole)
        if (
            not "wkt_centroid" in data
        ):  # this method is called from lsDialog that already has retrieved objects
            lookup_id = data["id"]
            data = None
            try:
                data = lookup_object(lookup_id, Projection.EPSG_28992)
            except PdokServicesNetworkException as ex:
                title = f"{PLUGIN_NAME} - HTTP Request Error"
                message = textwrap.dedent(
                    f"""an error occured while executing HTTP request, error:

                    {str(ex)}
                    """
                )
                self.show_error(message, title)
            if data is None:
                return
        self.fill_lookup_info(data)
        self.zoom_to_result(data)

    def set_pointer(self, point):
        self.remove_pointer()
        self.pointer = QgsVertexMarker(self.iface.mapCanvas())
        self.pointer.setColor(QColor(255, 0, 0))
        self.pointer.setIconSize(10)
        self.pointer.setPenWidth(2)
        self.pointer.setCenter(point)
        self.clean_action.setEnabled(True)

    def remove_pointer(self):
        if self.pointer is not None and self.pointer.scene() is not None:
            self.iface.mapCanvas().scene().removeItem(self.pointer)
            self.pointer = None
            self.clean_action.setEnabled(False)

    def info(self, msg=""):
        QgsMessageLog.logMessage("{}".format(msg), "PDOK-services Plugin", Qgis.Info)

    def show_error(self, message, title="PDOK plugin"):
        message = textwrap.dedent(
            message
        )  # textwrap.dedent nodig want anders leading whitespace issue, zie https://stackoverflow.com/a/1412728/1763690
        QMessageBox.critical(
            self.iface.mainWindow(),
            title,
            (message),
            QMessageBox.Ok,
            QMessageBox.Ok,
        )

    def show_warning(self, message, title="PDOK plugin"):
        message = textwrap.dedent(
            message
        )  # textwrap.dedent nodig want anders leading whitespace issue, zie https://stackoverflow.com/a/1412728/1763690
        QMessageBox.warning(
            self.iface.mainWindow(),
            title,
            (message),
            QMessageBox.Ok,
            QMessageBox.Ok,
        )
