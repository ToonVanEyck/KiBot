# -*- coding: utf-8 -*-
# Copyright (c) 2022 Salvador E. Tropea
# Copyright (c) 2022 Instituto Nacional de Tecnología Industrial
# Copyright (c) 2022 Albin Dennevi (create_pdf_from_pages)
# License: GPL-3.0
# Project: KiBot (formerly KiPlot)
# Base idea: https://gitlab.com/dennevi/Board2Pdf/ (Released as Public Domain)
import re
import os
import subprocess
from pcbnew import B_Cu, F_Cu, FromMM, IsCopperLayer, PLOT_CONTROLLER, PLOT_FORMAT_SVG, wxSize
from shutil import rmtree, which
from tempfile import NamedTemporaryFile, mkdtemp
from .svgutils.transform import fromstring
from .error import KiPlotConfigurationError
from .gs import GS
from .optionable import Optionable
from .out_base import VariantOptions
from .kicad.color_theme import load_color_theme
from .kicad.patch_svg import patch_svg_file
from .kicad.worksheet import Worksheet, WksError
from .kicad.config import KiConf
from .kicad.v5_sch import SchError
from .kicad.pcb import PCB
from .misc import CMD_PCBNEW_PRINT_LAYERS, URL_PCBNEW_PRINT_LAYERS, PDF_PCB_PRINT, MISSING_TOOL
from .kiplot import check_script, exec_with_retry, add_extra_options
from .macros import macros, document, output_class  # noqa: F401
from .layer import Layer, get_priority
from .__main__ import __version__
from . import PyPDF2
from . import log

logger = log.get_logger()
SVG2PDF = 'rsvg-convert'
PDF2PS = 'pdf2ps'
VIATYPE_THROUGH = 3
VIATYPE_BLIND_BURIED = 2
VIATYPE_MICROVIA = 1
POLY_FILL_STYLE = ("fill:{0}; fill-opacity:1.0; stroke:{0}; stroke-width:1; stroke-opacity:1; stroke-linecap:round; "
                   "stroke-linejoin:round;fill-rule:evenodd;")


def _run_command(cmd):
    logger.debug('- Executing: '+str(cmd))
    try:
        cmd_output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error('Failed to run %s, error %d', cmd[0], e.returncode)
        if e.output:
            logger.debug('Output from command: '+e.output.decode())
        exit(PDF_PCB_PRINT)
    if cmd_output.strip():
        logger.debug('- Output from command:\n'+cmd_output.decode())


def hex_to_rgb(value):
    """ Return (red, green, blue) in float between 0-1 for the color given as #rrggbb. """
    value = value.lstrip('#')
    rgb = tuple(int(value[i:i+2], 16) for i in range(0, 6, 2))
    rgb = (rgb[0]/255, rgb[1]/255, rgb[2]/255)
    alpha = int(value[6:], 16)/255 if len(value) == 8 else 1.0
    return rgb, alpha


def to_gray(color):
    avg = (color[0]+color[1]+color[2])/3
    return (avg, avg, avg)


def to_gray_hex(color):
    rgb, alpha = hex_to_rgb(color)
    avg = (rgb[0]+rgb[1]+rgb[2])/3
    avg_str = '%02X' % int(avg*255)
    return '#'+avg_str+avg_str+avg_str


def load_svg(file, color, colored_holes, holes_color, monochrome):
    with open(file, 'rt') as f:
        content = f.read()
    color = color[:7]
    if monochrome:
        color = to_gray_hex(color)
        holes_color = to_gray_hex(holes_color)
    if colored_holes:
        content = content.replace('#FFFFFF', '**black_hole**')
    if color != '#000000':
        # Files plotted
        content = content.replace('#000000', color)
        # Files generated by "Print"
        content = content.replace('stroke:rgb(0%,0%,0%)', 'stroke:'+color)
    if colored_holes:
        content = content.replace('**black_hole**', holes_color)
    return content


def get_width(svg):
    """ Finds the width in viewBox units """
    return float(svg.root.get('viewBox').split(' ')[2])


def to_inches(w):
    val = float(w[:-2])
    units = w[-2:]
    if units == 'cm':
        return val/2.54
    if units == 'pt':
        return val/72.0
    # Currently impossible for KiCad
    return val


def create_pdf_from_pages(input_files, output_fn):
    output = PyPDF2.PdfFileWriter()
    # Collect all pages
    open_files = []
    er = None
    for filename in input_files:
        try:
            file = open(filename, 'rb')
            open_files.append(file)
            pdf_reader = PyPDF2.PdfFileReader(file)
            page_obj = pdf_reader.getPage(0)
            page_obj.compressContentStreams()
            output.addPage(page_obj)
        except (IOError, ValueError, EOFError) as e:
            er = str(e)
        if er:
            raise KiPlotConfigurationError('Error reading `{}` ({})'.format(filename, er))
    # Write all pages to a file
    pdf_output = None
    try:
        pdf_output = open(output_fn, 'wb')
        output.write(pdf_output)
    except (IOError, ValueError, EOFError) as e:
        er = str(e)
    finally:
        if pdf_output:
            pdf_output.close()
    if er:
        raise KiPlotConfigurationError('Error creating `{}` ({})'.format(output_fn, er))
    # Close the files
    for f in open_files:
        f.close()


def svg_to_pdf(input_folder, svg_file, pdf_file):
    # Note: rsvg-convert uses 90 dpi but KiCad (and the docs I found) says SVG pt is 72 dpi
    cmd = [SVG2PDF, '-d', '72', '-p', '72', '-f', 'pdf', '-o', os.path.join(input_folder, pdf_file),
           os.path.join(input_folder, svg_file)]
    _run_command(cmd)


def svg_to_png(input_folder, svg_file, png_file, width):
    cmd = [SVG2PDF, '-w', str(width), '-f', 'png', '-o', os.path.join(input_folder, png_file),
           os.path.join(input_folder, svg_file)]
    _run_command(cmd)


def svg_to_eps(input_folder, svg_file, eps_file):
    cmd = [SVG2PDF, '-d', '72', '-p', '72', '-f', 'eps', '-o', os.path.join(input_folder, eps_file),
           os.path.join(input_folder, svg_file)]
    _run_command(cmd)


def pdf_to_ps(ps_file, output):
    cmd = [PDF2PS, ps_file, output]
    _run_command(cmd)


def create_pdf_from_svg_pages(input_folder, input_files, output_fn):
    svg_files = []
    for svg_file in input_files:
        pdf_file = svg_file.replace('.svg', '.pdf')
        svg_to_pdf(input_folder, svg_file, pdf_file)
        svg_files.append(os.path.join(input_folder, pdf_file))
    create_pdf_from_pages(svg_files, output_fn)


class LayerOptions(Layer):
    """ Data for a layer """
    def __init__(self):
        super().__init__()
        self._unkown_is_error = True
        with document:
            self.color = ""
            """ Color used for this layer """
            self.plot_footprint_refs = True
            """ Include the footprint references """
            self.plot_footprint_values = True
            """ Include the footprint values """
            self.force_plot_invisible_refs_vals = False
            """ Include references and values even when they are marked as invisible """

    def config(self, parent):
        super().config(parent)
        if self.color:
            self.validate_color('color')


class PagesOptions(Optionable):
    """ One page of the output document """
    def __init__(self):
        super().__init__()
        self._unkown_is_error = True
        with document:
            self.mirror = False
            """ Print mirrored (X axis inverted) """
            self.monochrome = False
            """ Print in gray scale """
            self.scaling = None
            """ [number=1.0] Scale factor (0 means autoscaling)"""
            self.title = ''
            """ Text used to replace the sheet title. %VALUE expansions are allowed.
                If it starts with `+` the text is concatenated """
            self.sheet = 'Assembly'
            """ Text to use for the `sheet` in the title block """
            self.sheet_reference_color = ''
            """ Color to use for the frame and title block """
            self.line_width = 0.1
            """ [0.02,2] For objects without width [mm] (KiCad 5) """
            self.negative_plot = False
            """ Invert black and white. Only useful for a single layer """
            self.exclude_pads_from_silkscreen = False
            """ Do not plot the component pads in the silk screen (KiCad 5.x only) """
            self.tent_vias = True
            """ Cover the vias """
            self.colored_holes = True
            """ Change the drill holes to be colored instead of white """
            self.holes_color = '#000000'
            """ Color used for the holes when `colored_holes` is enabled """
            self.sort_layers = False
            """ Try to sort the layers in the same order that uses KiCad for printing """
            self.layers = LayerOptions
            """ [list(dict)|list(string)|string] List of layers printed in this page.
                Order is important, the last goes on top """
        self._scaling_example = 1.0

    def config(self, parent):
        super().config(parent)
        if isinstance(self.layers, type):
            raise KiPlotConfigurationError("Missing `layers` list")
        # Fill the ID member for all the layers
        self.layers = LayerOptions.solve(self.layers)
        if self.sort_layers:
            self.layers.sort(key=lambda x: get_priority(x._id), reverse=True)
        if self.sheet_reference_color:
            self.validate_color('sheet_reference_color')
        if self.holes_color:
            self.validate_color('holes_color')
        if self.scaling is None:
            logger.error('Scale from parent')
            self.scaling = parent.scaling


class PCB_PrintOptions(VariantOptions):
    # Mappings to KiCad config values. They should be the same used in drill_marks.py
    _drill_marks_map = {'none': 0, 'small': 1, 'full': 2}
    _pad_colors = {'pad_color': 'pad_through_hole',
                   'via_color': 'via_through',
                   'micro_via_color': 'via_micro',
                   'blind_via_color': 'via_blind_buried'}

    def __init__(self):
        with document:
            self.output_name = None
            """ {output} """
            self.output = GS.def_global_output
            """ Filename for the output (%i=assembly, %x=pdf)/(%i=assembly_page_NN, %x=svg)"""
            self.hide_excluded = False
            """ Hide components in the Fab layer that are marked as excluded by a variant """
            self._drill_marks = 'full'
            """ What to use to indicate the drill places, can be none, small or full (for real scale) """
            self.color_theme = '_builtin_classic'
            """ Selects the color theme. Only applies to KiCad 6.
                To use the KiCad 6 default colors select `_builtin_default`.
                Usually user colors are stored as `user`, but you can give it another name """
            self.plot_sheet_reference = True
            """ Include the title-block (worksheet, frame, etc.) """
            self.sheet_reference_layout = ''
            """ Worksheet file (.kicad_wks) to use. Leave empty to use the one specified in the project """
            self.frame_plot_mechanism = 'internal'
            """ [gui,internal,plot] Plotting the frame from Python is problematic.
                This option selects a workaround strategy.
                gui: uses KiCad GUI to do it. Is slow but you get the correct frame.
                But it can't keep track of page numbers.
                internal: KiBot loads the `.kicad_wks` and does the drawing work.
                Best option, but some details are different from what the GUI generates.
                plot: uses KiCad Python API. Only available for KiCad 6.
                You get the default frame and some substitutions doesn't work """
            self.pages = PagesOptions
            """ [list(dict)] List of pages to include in the output document.
                Each page contains one or more layers of the PCB """
            self.title = ''
            """ Text used to replace the sheet title. %VALUE expansions are allowed.
                If it starts with `+` the text is concatenated """
            self.format = 'PDF'
            """ [PDF,SVG,PNG,EPS,PS] Format for the output file/s.
                Note that for PS you need `ghostscript` which isn't part of the default docker images """
            self.png_width = 1280
            """ Width of the PNG in pixels """
            self.colored_pads = True
            """ Plot through-hole in a different color. Like KiCad GUI does """
            self.pad_color = ''
            """ Color used for `colored_pads` """
            self.colored_vias = True
            """ Plot vias in a different color. Like KiCad GUI does """
            self.via_color = ''
            """ Color used for through-hole `colored_vias` """
            self.micro_via_color = ''
            """ Color used for micro `colored_vias` """
            self.blind_via_color = ''
            """ Color used for blind/buried `colored_vias` """
            self.keep_temporal_files = False
            """ Store the temporal page and layer files in the output dir and don't delete them """
            self.force_edge_cuts = False
            """ Add the `Edge.Cuts` to all the pages """
            self.scaling = 1.0
            """ Default scale factor (0 means autoscaling)"""
        super().__init__()
        self._expand_id = 'assembly'

    @property
    def drill_marks(self):
        return self._drill_marks

    @drill_marks.setter
    def drill_marks(self, val):
        if val not in self._drill_marks_map:
            raise KiPlotConfigurationError("Unknown drill mark type: {}".format(val))
        self._drill_marks = val

    def config(self, parent):
        super().config(parent)
        if isinstance(self.pages, type):
            raise KiPlotConfigurationError("Missing `pages` list")
        self._color_theme = load_color_theme(self.color_theme)
        if self._color_theme is None:
            raise KiPlotConfigurationError("Unable to load `{}` color theme".format(self.color_theme))
        # Assign a color if none was defined
        layer_id2color = self._color_theme.layer_id2color
        for p in self.pages:
            for la in p.layers:
                if not la.color:
                    if la._id in layer_id2color:
                        la.color = layer_id2color[la._id]
                    else:
                        la.color = "#000000"
        self._drill_marks = PCB_PrintOptions._drill_marks_map[self._drill_marks]
        self._expand_ext = self.format.lower()
        for member, color in self._pad_colors.items():
            if getattr(self, member):
                self.validate_color(member)
            else:
                setattr(self, member, getattr(self._color_theme, color))
        if self.frame_plot_mechanism == 'plot' and GS.ki5():
            raise KiPlotConfigurationError("You can't use `plot` for `frame_plot_mechanism` with KiCad 5. It will crash.")
        KiConf.init(GS.pcb_file)
        if self.sheet_reference_layout:
            self.sheet_reference_layout = KiConf.expand_env(self.sheet_reference_layout)
            if not os.path.isfile(self.sheet_reference_layout):
                raise KiPlotConfigurationError("Missing page layout file: "+self.sheet_reference_layout)

    def filter_components(self):
        if not self._comps:
            return
        comps_hash = self.get_refs_hash()
        self.cross_modules(GS.board, comps_hash)
        self.remove_paste_and_glue(GS.board, comps_hash)
        if self.hide_excluded:
            self.remove_fab(GS.board, comps_hash)

    def unfilter_components(self):
        if not self._comps:
            return
        comps_hash = self.get_refs_hash()
        self.uncross_modules(GS.board, comps_hash)
        self.restore_paste_and_glue(GS.board, comps_hash)
        if self.hide_excluded:
            self.restore_fab(GS.board, comps_hash)

    def get_targets(self, out_dir):
        if self.format in ['SVG', 'PNG', 'EPS']:
            files = []
            for n in range(len(self.pages)):
                id = self._expand_id+('_page_%02d' % (n+1))
                files.append(self.expand_filename(out_dir, self.output, id, self._expand_ext))
            return files
        return [self._parent.expand_filename(out_dir, self.output)]

    def clear_layer(self, layer):
        tmp_layer = GS.board.GetLayerID(GS.work_layer)
        cleared_layer = GS.board.GetLayerID(layer)
        moved = []
        for g in GS.board.GetDrawings():
            if g.GetLayer() == cleared_layer:
                g.SetLayer(tmp_layer)
                moved.append(g)
        for m in GS.get_modules():
            for gi in m.GraphicalItems():
                if gi.GetLayer() == cleared_layer:
                    gi.SetLayer(tmp_layer)
                    moved.append(gi)
        self.moved_items = moved
        self.cleared_layer = cleared_layer

    def restore_layer(self):
        for g in self.moved_items:
            g.SetLayer(self.cleared_layer)

    def plot_frame_api(self, pc, po, p):
        """ KiCad 6 can plot the frame because it loads the worksheet format.
            But not the one from the project, just a default """
        self.clear_layer('Edge.Cuts')
        po.SetPlotFrameRef(True)
        po.SetScale(1.0)
        po.SetNegative(False)
        pc.SetLayer(self.cleared_layer)
        pc.OpenPlotfile('frame', PLOT_FORMAT_SVG, p.sheet)
        pc.PlotLayer()
        self.restore_layer()

    def fill_kicad_vars(self, page, pages, p):
        vars = {}
        vars['KICAD_VERSION'] = 'KiCad E.D.A. '+GS.kicad_version+' + KiBot v'+__version__
        vars['#'] = str(page)
        vars['##'] = str(pages)
        GS.load_pcb_title_block()
        for num in range(9):
            vars['COMMENT'+str(num+1)] = GS.pcb_com[num]
        vars['COMPANY'] = GS.pcb_comp
        vars['ISSUE_DATE'] = GS.pcb_date
        vars['REVISION'] = GS.pcb_rev
        # The set_title member already took care of modifying the board value
        tb = GS.board.GetTitleBlock()
        vars['TITLE'] = tb.GetTitle()
        vars['FILENAME'] = GS.pcb_basename+'.kicad_pcb'
        vars['SHEETNAME'] = p.sheet
        layer = ''
        for la in p.layers:
            if len(layer):
                layer += '+'
            layer = layer+la.layer
        vars['LAYER'] = layer
        vars['PAPER'] = self.paper
        return vars

    def plot_frame_internal(self, pc, po, p, page, pages):
        """ Here we plot the frame manually """
        self.clear_layer('Edge.Cuts')
        po.SetPlotFrameRef(False)
        po.SetScale(1.0)
        po.SetNegative(False)
        pc.SetLayer(self.cleared_layer)
        # Load the WKS
        error = None
        try:
            ws = Worksheet.load(self.layout)
        except (WksError, SchError) as e:
            error = str(e)
        if error:
            raise KiPlotConfigurationError('Error reading `{}` ({})'.format(self.layout, error))
        tb_vars = self.fill_kicad_vars(page, pages, p)
        ws.draw(GS.board, self.cleared_layer, page, self.paper_w, self.paper_h, tb_vars)
        pc.OpenPlotfile('frame', PLOT_FORMAT_SVG, p.sheet)
        pc.PlotLayer()
        ws.undraw(GS.board)
        self.restore_layer()
        # We need to plot the images in a separated pass
        self.last_worksheet = ws

    def plot_frame_gui(self, dir_name, layer='Edge.Cuts'):
        """ KiCad 5 crashes if we try to print the frame.
            So we print a frame using pcbnew_do export.
            We use SVG output to then generate a vectorized PDF. """
        output = os.path.join(dir_name, GS.pcb_basename+"-frame.svg")
        check_script(CMD_PCBNEW_PRINT_LAYERS, URL_PCBNEW_PRINT_LAYERS, '1.6.7')
        # Move all the drawings away
        # KiCad 5 always prints Edge.Cuts, so we make it empty
        self.clear_layer(layer)
        # Save the PCB
        pcb_name, pcb_dir = self.save_tmp_dir_board('pcb_print')
        # Restore the layer
        self.restore_layer()
        # Output file name
        cmd = [CMD_PCBNEW_PRINT_LAYERS, 'export', '--output_name', output, '--monochrome', '--svg', '--pads', '0',
               pcb_name, dir_name, layer]
        cmd, video_remove = add_extra_options(cmd)
        # Execute it
        ret = exec_with_retry(cmd)
        # Remove the temporal PCB
        logger.debug('Removing temporal PCB used for frame `{}`'.format(pcb_dir))
        rmtree(pcb_dir)
        if ret:
            logger.error(CMD_PCBNEW_PRINT_LAYERS+' returned %d', ret)
            exit(PDF_PCB_PRINT)
        if video_remove:
            video_name = os.path.join(self.expand_filename_pcb(GS.out_dir), 'pcbnew_export_screencast.ogv')
            if os.path.isfile(video_name):
                os.remove(video_name)
        patch_svg_file(output, remove_bkg=True)

    def plot_pads(self, la, pc, p, filelist):
        id = la._id
        logger.debug('- Plotting pads for layer {} ({})'.format(la.layer, id))
        # Make invisible anything but through-hole pads
        tmp_layer = GS.board.GetLayerID(GS.work_layer)
        moved = []
        removed = []
        vias = []
        wxSize(0, 0)
        for m in GS.get_modules():
            for gi in m.GraphicalItems():
                if gi.GetLayer() == id:
                    gi.SetLayer(tmp_layer)
                    moved.append(gi)
            for pad in m.Pads():
                dr = pad.GetDrillSize()
                if dr.x:
                    continue
                layers = pad.GetLayerSet()
                layers.removeLayer(id)
                pad.SetLayerSet(layers)
                removed.append(pad)
        for e in GS.board.GetDrawings():
            if e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        for e in list(GS.board.Zones()):
            if e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        via_type = 'VIA' if GS.ki5() else 'PCB_VIA'
        for e in GS.board.GetTracks():
            if e.GetClass() == via_type:
                vias.append((e, e.GetDrill(), e.GetWidth()))
                e.SetDrill(0)
                e.SetWidth(0)
            elif e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        # Plot the layer
        # pc.SetLayer(id) already selected
        suffix = la.suffix+'_pads'
        pc.OpenPlotfile(suffix, PLOT_FORMAT_SVG, p.sheet)
        pc.PlotLayer()
        # Restore everything
        for e in moved:
            e.SetLayer(id)
        for pad in removed:
            layers = pad.GetLayerSet()
            layers.addLayer(id)
            pad.SetLayerSet(layers)
        for (via, drill, width) in vias:
            via.SetDrill(drill)
            via.SetWidth(width)
        # Add it to the list
        filelist.append((GS.pcb_basename+"-"+suffix+".svg", self.pad_color))

    def plot_vias(self, la, pc, p, filelist, via_t, via_c):
        id = la._id
        logger.debug('- Plotting vias for layer {} ({})'.format(la.layer, id))
        # Make invisible anything but vias
        tmp_layer = GS.board.GetLayerID(GS.work_layer)
        moved = []
        removed = []
        vias = []
        wxSize(0, 0)
        for m in GS.get_modules():
            for gi in m.GraphicalItems():
                if gi.GetLayer() == id:
                    gi.SetLayer(tmp_layer)
                    moved.append(gi)
            for pad in m.Pads():
                layers = pad.GetLayerSet()
                layers.removeLayer(id)
                pad.SetLayerSet(layers)
                removed.append(pad)
        for e in GS.board.GetDrawings():
            if e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        for e in list(GS.board.Zones()):
            if e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        via_type = 'VIA' if GS.ki5() else 'PCB_VIA'
        for e in GS.board.GetTracks():
            if e.GetClass() == via_type:
                if e.GetViaType() == via_t:
                    # Include it, but ...
                    if not e.IsOnLayer(id):
                        # This is a via that doesn't drill this layer
                        # Lamentably KiCad will draw a drill here
                        # So we create a "patch" for the hole
                        top = e.TopLayer()
                        bottom = e.BottomLayer()
                        w = e.GetWidth()
                        d = e.GetDrill()
                        vias.append((e, d, w, top, bottom))
                        e.SetWidth(d)
                        e.SetDrill(1)
                        e.SetTopLayer(F_Cu)
                        e.SetBottomLayer(B_Cu)
                else:
                    top = e.TopLayer()
                    bottom = e.BottomLayer()
                    w = e.GetWidth()
                    d = e.GetDrill()
                    vias.append((e, d, w, top, bottom))
                    e.SetWidth(0)
            elif e.GetLayer() == id:
                e.SetLayer(tmp_layer)
                moved.append(e)
        # Plot the layer
        suffix = la.suffix+'_vias_'+str(via_t)
        pc.OpenPlotfile(suffix, PLOT_FORMAT_SVG, p.sheet)
        pc.PlotLayer()
        # Restore everything
        for e in moved:
            e.SetLayer(id)
        for pad in removed:
            layers = pad.GetLayerSet()
            layers.addLayer(id)
            pad.SetLayerSet(layers)
        for (via, drill, width, top, bottom) in vias:
            via.SetDrill(drill)
            via.SetWidth(width)
            via.SetTopLayer(top)
            via.SetBottomLayer(bottom)
        # Add it to the list
        filelist.append((GS.pcb_basename+"-"+suffix+".svg", via_c))

    def add_frame_images(self, svg, monochrome):
        if (not self.plot_sheet_reference or not self.frame_plot_mechanism == 'internal' or
           not self.last_worksheet.has_images):
            return
        if monochrome:
            if which('convert') is None:
                logger.error('`convert` not installed. install `imagemagick` or equivalent')
                exit(MISSING_TOOL)
            for img in self.last_worksheet.images:
                with NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as f:
                    f.write(img.data)
                    fname = f.name
                dest = fname.replace('.png', '_gray.png')
                _run_command(['convert', fname, '-set', 'colorspace', 'Gray', '-separate', '-average', dest])
                with open(dest, 'rb') as f:
                    img.data = f.read()
                os.remove(fname)
                os.remove(dest)
        self.last_worksheet.add_images_to_svg(svg)

    def fill_polygons(self, svg, color):
        """ I don't know how to generate filled polygons on KiCad 5.
            So here we look for KiCad 5 unfilled polygons and transform them into filled polygons.
            Note that all polygons in the frame are filled. """
        logger.debug('- Filling KiCad 5 polygons')
        cnt = 0
        ml_coord = re.compile(r'M(\d+) (\d+) L(\d+) (\d+)')
        # Scan the SVG
        for e in svg.root:
            if e.tag.endswith('}g'):
                # This is a graphic
                if len(e) < 2:
                    # Polygons have at least 2 paths
                    continue
                # Check that all elements are paths and that they have the coordinates in 'd'
                all_path = True
                for c in e:
                    if not c.tag.endswith('}path') or c.get('d') is None:
                        all_path = False
                        break
                if all_path:
                    # Ok, this is a KiCad 5 polygon
                    # Create a list with all the points
                    coords = 'M '
                    all_coords = True
                    first = True
                    for c in e:
                        coord = c.get('d')
                        res = ml_coord.match(coord)
                        if not res:
                            # Discard it if we can't understand the coordinates
                            all_coords = False
                            break
                        coords += res.group(1)+','+res.group(2)+'\n'
                        if first:
                            start = res.group(1)+','+res.group(2)
                            first = False
                    if all_coords:
                        # Ok, we have all the points
                        end = res.group(3)+','+res.group(4)
                        if start == end:
                            # Must be a closed polygon
                            coords += end+'\nZ'
                            # Make the first a single filled polygon
                            e[0].set('style', POLY_FILL_STYLE.format(color))
                            e[0].set('d', coords)
                            # Remove the rest
                            for c in e[1:]:
                                e.remove(c)
                            cnt = cnt+1
        logger.debug('- Filled {} polygons'.format(cnt))

    def merge_svg(self, input_folder, input_files, output_folder, output_file, p):
        """ Merge all pages into one """
        first = True
        for (file, color) in input_files:
            file = os.path.join(input_folder, file)
            new_layer = fromstring(load_svg(file, color, p.colored_holes, p.holes_color, p.monochrome))
            width = get_width(new_layer)
            if GS.ki5() and file.endswith('frame.svg'):
                if p.monochrome:
                    color = to_gray_hex(color)
                self.fill_polygons(new_layer, color)
            if first:
                svg_out = new_layer
                # This is the width declared at the beginning of the file
                base_width = width
                phys_width = to_inches(new_layer.width)
                first = False
                self.add_frame_images(svg_out, p.monochrome)
            else:
                root = new_layer.getroot()
                # Adjust the coordinates of this section to the main width
                scale = base_width/width
                if scale != 1.0:
                    logger.debug(' - Scaling {} by {}'.format(file, scale))
                    for e in root:
                        e.scale(scale)
                svg_out.append([root])
        svg_out.save(os.path.join(output_folder, output_file))
        return phys_width

    def find_paper_size(self):
        pcb = PCB.load(GS.pcb_file)
        self.paper_w = pcb.paper_w
        self.paper_h = pcb.paper_h
        self.paper = pcb.paper

    def plot_extra_cu(self, id, la, pc, p, filelist):
        """ Plot pads and vias to make them different """
        if id >= F_Cu and id <= B_Cu:
            if self.colored_pads:
                self.plot_pads(la, pc, p, filelist)
            if self.colored_vias:
                self.plot_vias(la, pc, p, filelist, VIATYPE_THROUGH, self.via_color)
                self.plot_vias(la, pc, p, filelist, VIATYPE_BLIND_BURIED, self.blind_via_color)
                self.plot_vias(la, pc, p, filelist, VIATYPE_MICROVIA, self.micro_via_color)

    def generate_output(self, output):
        if self.format != 'SVG' and which(SVG2PDF) is None:
            logger.error('`{}` not installed. Install `librsvg2-bin` or equivalent'.format(SVG2PDF))
            exit(MISSING_TOOL)
        if self.format == 'PS' and which(PDF2PS) is None:
            logger.error('`{}` not installed. '.format(PDF2PS))
            logger.error('Install `librsvg2-bin` or equivalent')
            exit(MISSING_TOOL)
        output_dir = os.path.dirname(output)
        if self.keep_temporal_files:
            temp_dir_base = output_dir
        else:
            temp_dir_base = mkdtemp(prefix='tmp-kibot-pcb_print-')
        logger.debug('Starting to generate `{}`'.format(output))
        logger.debug('- Temporal dir: {}'.format(temp_dir_base))
        self.find_paper_size()
        if self.sheet_reference_layout:
            layout = self.sheet_reference_layout
        else:
            # Find the layout file
            layout = KiConf.fix_page_layout(GS.pro_file, dry=True)[1]
        if not layout or not os.path.isfile(layout):
            layout = os.path.abspath(os.path.join(os.path.dirname(__file__), 'kicad_layouts', 'default.kicad_wks'))
        logger.debug('- Using layout: '+layout)
        self.layout = layout
        # Plot options
        pc = PLOT_CONTROLLER(GS.board)
        po = pc.GetPlotOptions()
        # Set General Options:
        po.SetExcludeEdgeLayer(True)   # We plot it separately
        po.SetUseAuxOrigin(False)
        po.SetAutoScale(False)
        # Helpers for force_edge_cuts
        if self.force_edge_cuts:
            edge_layer = LayerOptions.create_layer('Edge.Cuts')
            edge_id = edge_layer._id
            layer_id2color = self._color_theme.layer_id2color
            if edge_id in layer_id2color:
                edge_layer.color = layer_id2color[edge_id]
            else:
                edge_layer.color = "#000000"
        # Generate the output
        pages = []
        for n, p in enumerate(self.pages):
            # Use a dir for each page, avoid overwriting files, just for debug purposes
            page_str = "%02d" % (n+1)
            temp_dir = os.path.join(temp_dir_base, page_str)
            os.makedirs(temp_dir, exist_ok=True)
            po.SetOutputDirectory(temp_dir)
            # Adapt the title
            self.set_title(p.title if p.title else self.title)
            # 1) Plot all layers to individual PDF files (B&W)
            po.SetPlotFrameRef(False)   # We plot it separately
            po.SetMirror(p.mirror)
            po.SetScale(p.scaling)
            po.SetNegative(p.negative_plot)
            po.SetPlotViaOnMaskLayer(not p.tent_vias)
            if GS.ki5():
                po.SetLineWidth(FromMM(p.line_width))
                po.SetPlotPadsOnSilkLayer(not p.exclude_pads_from_silkscreen)
            filelist = []
            if self.force_edge_cuts and next(filter(lambda x: x._id == edge_id, p.layers), None) is None:
                p.layers.append(edge_layer)
            for la in p.layers:
                id = la._id
                logger.debug('- Plotting layer {} ({})'.format(la.layer, id))
                po.SetPlotReference(la.plot_footprint_refs)
                po.SetPlotValue(la.plot_footprint_values)
                po.SetPlotInvisibleText(la.force_plot_invisible_refs_vals)
                # Avoid holes on non-copper layers
                po.SetDrillMarksType(self._drill_marks if IsCopperLayer(id) else 0)
                pc.SetLayer(id)
                pc.OpenPlotfile(la.suffix, PLOT_FORMAT_SVG, p.sheet)
                pc.PlotLayer()
                filelist.append((GS.pcb_basename+"-"+la.suffix+".svg", la.color))
                self.plot_extra_cu(id, la, pc, p, filelist)
            # 2) Plot the frame using an empty layer and 1.0 scale
            po.SetMirror(False)
            if self.plot_sheet_reference:
                logger.debug('- Plotting the frame')
                if self.frame_plot_mechanism == 'gui':
                    self.plot_frame_gui(temp_dir)
                elif self.frame_plot_mechanism == 'plot':
                    self.plot_frame_api(pc, po, p)
                else:   # internal
                    self.plot_frame_internal(pc, po, p, len(pages)+1, len(self.pages))
                color = p.sheet_reference_color if p.sheet_reference_color else self._color_theme.pcb_frame
                filelist.append((GS.pcb_basename+"-frame.svg", color))
            pc.ClosePlot()
            # 3) Stack all layers in one file
            if self.format == 'SVG':
                id = self._expand_id+('_page_'+page_str)
                assembly_file = self.expand_filename(output_dir, self.output, id, self._expand_ext)
            else:
                assembly_file = GS.pcb_basename+".svg"
            logger.debug('- Merging layers to {}'.format(assembly_file))
            self.merge_svg(temp_dir, filelist, temp_dir, assembly_file, p)
            if self.format in ['PNG', 'EPS']:
                id = self._expand_id+('_page_'+page_str)
                out_file = self.expand_filename(output_dir, self.output, id, self._expand_ext)
                if self.format == 'PNG':
                    svg_to_png(temp_dir, assembly_file, out_file, self.png_width)
                else:
                    svg_to_eps(temp_dir, assembly_file, out_file)
            pages.append(os.path.join(page_str, assembly_file))
            self.restore_title()
        # Join all pages in one file
        if self.format in ['PDF', 'PS']:
            logger.debug('- Creating output file {}'.format(output))
            if self.format == 'PDF':
                create_pdf_from_svg_pages(temp_dir_base, pages, output)
            else:
                ps_file = os.path.join(temp_dir, GS.pcb_basename+'.ps')
                create_pdf_from_svg_pages(temp_dir_base, pages, ps_file)
                pdf_to_ps(ps_file, output)
        # Remove the temporal files
        if not self.keep_temporal_files:
            rmtree(temp_dir_base)
        logger.debug('Finished generating `{}`'.format(output))

    def run(self, output):
        super().run(output)
        self.filter_components()
        self.generate_output(output)
        self.unfilter_components()


@output_class
class PCB_Print(BaseOutput):  # noqa: F821
    """ PCB Print
        Prints the PCB using a mechanism that is more flexible than `pdf_pcb_print` and `svg_pcb_print`.
        Supports PDF, SVG, PNG, EPS and PS formats.
        KiCad 5: including the frame is slow.
        KiCad 6: for custom frames use the `enable_ki6_frame_fix`, is slow. """
    def __init__(self):
        super().__init__()
        with document:
            self.options = PCB_PrintOptions
            """ [dict] Options for the `pcb_print` output """
