"""Layers of the annotation stack.

Every mark stays data (a stroke's points, a fill's outline), so a layer is just
the lists of marks that belong to it plus how they are put onto the picture:
visibility, opacity and blend mode. All of that can change at any time, long
after the marks were drawn.

The viewer's ``drawn_*`` lists are the *active* layer's lists, the very same
objects (see ``RandomImageViewer._bind_layer``), so the drawing tools only ever
see "the marks" and need to know nothing about layers.
"""

from PySide6.QtGui import QPainter

# What a layer holds. _undo_stack is per layer because it also records the
# order its marks were made in, which is the order they are painted in.
DRAWN_FIELDS = ('drawn_lines', 'drawn_horizontal_lines', 'drawn_free_lines',
                'drawn_free_strokes', 'drawn_fills', 'drawn_images')
MARK_FIELDS = DRAWN_FIELDS + ('erase_strokes', '_undo_stack')

BLEND_MODES = {
    'Normal': QPainter.CompositionMode_SourceOver,
    'Multiply': QPainter.CompositionMode_Multiply,
    'Screen': QPainter.CompositionMode_Screen,
    'Overlay': QPainter.CompositionMode_Overlay,
    'Soft Light': QPainter.CompositionMode_SoftLight,
    'Hard Light': QPainter.CompositionMode_HardLight,
    'Darken': QPainter.CompositionMode_Darken,
    'Lighten': QPainter.CompositionMode_Lighten,
    'Color Dodge': QPainter.CompositionMode_ColorDodge,
    'Color Burn': QPainter.CompositionMode_ColorBurn,
    'Difference': QPainter.CompositionMode_Difference,
}


class Layer:
    def __init__(self, name):
        self.name = name
        self.visible = True
        self.opacity = 100      # percent
        self.blend = 'Normal'
        self.effects = True     # the view's effects (LUT, curves ...) apply to it too
        # ⛰ Impasto: None (flat paint), or its settings (see impasto.DEFAULTS):
        # the paint lit as relief - bristle grooves, raised edges.
        self.impasto = None
        self.impasto_lit = None     # (key, image): the last lit image, see _impasto_layer
        # A canvas layer: a sheet of paper (this colour) over the picture,
        # under the layer's own marks. None: a plain, see-through layer.
        self.fill = None
        self.marks = {field: [] for field in MARK_FIELDS}
        # The image as it was before each of the last few marks, so undo
        # takes one off without laying the whole drawing again (see
        # RandomImageViewer._layer_image). Each: {'key', 'count', 'last', 'image'}.
        self.raster_steps = []
        self.raster = None      # the marks as last painted, see _layer_image

    @property
    def raster(self):
        return self._raster

    @raster.setter
    def raster(self, value):
        # Thrown away (marks moved, cleared): what was kept of it goes too.
        if value is None:
            self.raster_steps = []
        self._raster = value

    def has_marks(self):
        return any(self.marks[field] for field in DRAWN_FIELDS)

    def is_plain(self):
        """True when the marks can be painted straight onto the picture.

        Anything else needs a transparent image of its own: opacity and blend
        apply to the layer as a whole, and erasing must remove the layer's own
        pixels without touching the picture or the layers below.
        """
        return (self.opacity == 100 and self.blend == 'Normal' and not self.impasto
                and not self.marks['erase_strokes'] and self.fill is None
                and not any(getattr(s, 'erase', False) for s in self.marks['drawn_free_strokes']))

    def has_content(self):
        """Marks, or paper: anything this layer puts on screen."""
        return self.fill is not None or self.has_marks()

    def clear(self):
        """Its marks and its paper go (with the drawing they belong to); the
        layer itself, its blend, opacity and effects stay for the next one."""
        for marks in self.marks.values():
            marks.clear()
        self.fill = None
        self.raster = None

    def signature(self):
        """Everything about this layer that changes what is on screen."""
        counts = '.'.join(str(len(self.marks[field])) for field in MARK_FIELDS)
        paper = self.fill.rgba() if self.fill is not None else '-'
        impasto = '.'.join(str(v) for v in sorted(self.impasto.items())) if self.impasto else '-'
        return (f"{id(self)}-{int(self.visible)}-{self.opacity}-{self.blend}-{int(self.effects)}"
                f"-{paper}-{counts}-{impasto}")
