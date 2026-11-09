bl_info = {
    "name": "PoseMesh Hide — Geometry Selection Across Modes",
    "author": "OpenAI&SexyGuy",
    "version": (0, 1, 1),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > PoseMesh",
    "description": "Hide mesh vertices, edges and faces while posing without separating the mesh",
    "category": "3D View",
}

import bpy
import bmesh
import gpu
import time
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector

# This add-on intentionally does not change bone parenting, vertex weights or
# topology in the source Mesh. The evaluated preview is filtered by Geometry Nodes.
MASK = {"POINT": "_pmh_hide_v", "EDGE": "_pmh_hide_e", "FACE": "_pmh_hide_f"}
ORIG = {"POINT": "_pmh_original_v", "EDGE": "_pmh_original_e", "FACE": "_pmh_original_f"}
SEL = {"POINT": "_pmh_selected_v", "EDGE": "_pmh_selected_e", "FACE": "_pmh_selected_f"}
GROUP_TAG = "_pmh_nodes_v1"
TAP_TIMEOUT = 0.33
_keymaps = []
_active_pick = None
_in_sync = False


def current_mode(context):
    m = getattr(context, "mode", "OBJECT")
    if m == "POSE":
        return "POSE"
    if m == "EDIT_MESH":
        return "EDIT"
    if m == "OBJECT":
        return "OBJECT"
    return "OTHER"


def enabled(settings, mode):
    return bool({"POSE": settings.affect_pose,
                 "OBJECT": settings.affect_object,
                 "EDIT": settings.affect_edit}.get(mode, False))


def is_bound(mesh_obj, armature):
    if mesh_obj.parent == armature:
        return True
    return any(mod.type == 'ARMATURE' and mod.object == armature
               for mod in mesh_obj.modifiers)


def choose_target(context):
    settings = context.scene.pmh_settings
    if settings.target and settings.target.type == 'MESH':
        return settings.target
    active = context.active_object
    if active and active.type == 'MESH':
        return active
    if active and active.type == 'ARMATURE':
        meshes = [obj for obj in context.scene.objects
                  if obj.type == 'MESH' and is_bound(obj, active)]
        if meshes:
            return meshes[0]
    return None


def get_modifier(obj):
    # Blender 5.2: NodesModifier does not support IDProperties. A custom
    # property on the NodeTree (an ID datablock) remains supported.
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        group = mod.node_group
        if group is not None and group.get(GROUP_TAG, False):
            return mod
    return None


def make_node_group():
    # The three stored indices let viewport picking map evaluated polygons,
    # edges and vertices back to the original source mesh after filtering.
    group = bpy.data.node_groups.new("PoseMesh Hide | Filter and Origin IDs", 'GeometryNodeTree')
    group[GROUP_TAG] = True
    group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes, links = group.nodes, group.links
    src = nodes.new('NodeGroupInput')
    src.location = (-1050, 160)
    dst = nodes.new('NodeGroupOutput')
    dst.location = (1150, 160)
    prev = src.outputs['Geometry']

    # First store original IDs for all domains, before deleting anything.
    for i, domain in enumerate(("POINT", "EDGE", "FACE")):
        store = nodes.new('GeometryNodeStoreNamedAttribute')
        store.domain = domain
        store.data_type = 'INT'
        store.inputs['Name'].default_value = ORIG[domain]
        store.location = (-800 + i * 220, 160)
        idx = nodes.new('GeometryNodeInputIndex')
        idx.location = (-800 + i * 220, -40)
        links.new(idx.outputs['Index'], store.inputs['Value'])
        links.new(prev, store.inputs['Geometry'])
        prev = store.outputs['Geometry']

    # Hide faces, edges and vertices independently and exactly by domain.
    # 'ALL' removes associated dependent elements where necessary.
    for i, domain in enumerate(("FACE", "EDGE", "POINT")):
        attr = nodes.new('GeometryNodeInputNamedAttribute')
        attr.data_type = 'INT'
        attr.inputs['Name'].default_value = MASK[domain]
        attr.location = (-50 + i * 300, -100)
        cmp_node = nodes.new('ShaderNodeMath')
        cmp_node.operation = 'GREATER_THAN'
        cmp_node.inputs[1].default_value = 0.0
        cmp_node.location = (130 + i * 300, -100)
        links.new(attr.outputs['Attribute'], cmp_node.inputs[0])
        deleter = nodes.new('GeometryNodeDeleteGeometry')
        deleter.domain = domain
        deleter.mode = 'ALL'
        deleter.location = (130 + i * 300, 160)
        links.new(cmp_node.outputs['Value'], deleter.inputs['Selection'])
        links.new(prev, deleter.inputs['Geometry'])
        prev = deleter.outputs['Geometry']
    links.new(prev, dst.inputs['Geometry'])
    return group


def create_attributes(mesh):
    for domain, name in MASK.items():
        attr = mesh.attributes.get(name)
        if attr is None:
            mesh.attributes.new(name, type='INT', domain=domain)
        elif attr.data_type != 'INT' or attr.domain != domain:
            raise RuntimeError(f"Attribute conflict: {name}")


def ensure_setup(context, obj):
    if obj is None or obj.type != 'MESH':
        raise RuntimeError("Choose a mesh in the PoseMesh panel")
    if obj.library or obj.data.library:
        raise RuntimeError("Linked, read-only meshes are unsupported")
    if obj.data.users > 1:
        if obj.mode == 'EDIT':
            raise RuntimeError("Make the mesh data single-user before using in Edit Mode")
        obj.data = obj.data.copy()
    # Mesh attributes created through the RNA Mesh API are unsupported in edit
    # mode. BMesh INT custom-data layers are equivalent and write back on save.
    if obj.mode != 'EDIT':
        create_attributes(obj.data)
    else:
        bm = bmesh.from_edit_mesh(obj.data)
        for domain in MASK:
            seq = {"POINT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[domain]
            if not seq.layers.int.get(MASK[domain]):
                seq.layers.int.new(MASK[domain])
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    mod = get_modifier(obj)
    if mod is None:
        group = next((ng for ng in bpy.data.node_groups if ng.get(GROUP_TAG)), None)
        if group is None:
            group = make_node_group()
        mod = obj.modifiers.new("PoseMesh Hide (preview only)", type='NODES')
        mod.node_group = group
        mod.show_render = False
        mod.show_in_editmode = True
    # Keeping the modifier at the end preserves the shape from the armature.
    index = list(obj.modifiers).index(mod)
    if index < len(obj.modifiers) - 1:
        obj.modifiers.move(index, len(obj.modifiers) - 1)
    return mod


def sync_visibility(context=None):
    global _in_sync
    if _in_sync:
        return
    context = context or bpy.context
    scene = getattr(context, 'scene', None)
    if not scene or not hasattr(scene, 'pmh_settings'):
        return
    cfg = scene.pmh_settings
    visible = enabled(cfg, current_mode(context))
    _in_sync = True
    try:
        for obj in scene.objects:
            if obj.type != 'MESH':
                continue
            mod = get_modifier(obj)
            if mod and mod.show_viewport != visible:
                mod.show_viewport = visible
    finally:
        _in_sync = False


def on_scope_change(self, context):
    sync_visibility(context)


def on_xray_alpha(self, context):
    if context.area and context.area.type == 'VIEW_3D':
        context.space_data.shading.xray_alpha = self.xray_alpha


def _target_poll(self, obj):
    return obj.type == 'MESH'


class PMH_Settings(bpy.types.PropertyGroup):
    target: PointerProperty(name="Target Mesh", type=bpy.types.Object, poll=_target_poll,
                            description="The mesh to edit while posing the armature")
    affect_object: BoolProperty(name="Object", default=True, update=on_scope_change)
    affect_pose: BoolProperty(name="Pose", default=True, update=on_scope_change)
    affect_edit: BoolProperty(name="Edit", default=True, update=on_scope_change)
    select_domain: EnumProperty(
        name="Selection", default='FACE',
        items=[('POINT', "Vertex", "Select vertices"),
               ('EDGE', "Edge", "Select edges"),
               ('FACE', "Face", "Select faces")])
    pick_through: BoolProperty(name="Select Through", default=False,
                               description="Allow selection behind visible faces")
    xray_alpha: FloatProperty(name="Transparency", default=.40, min=.05, max=1.0,
                              update=on_xray_alpha,
                              description="X-Ray shading opacity")


def read_selected(context, obj, domain):
    if context.mode == 'EDIT_MESH' and obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        seq = {"POINT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[domain]
        seq.ensure_lookup_table()
        seq.index_update()
        return {i for i, elem in enumerate(seq) if elem.select and not elem.hide}
    return set(int(x) for x in obj.get(SEL[domain], []))


def write_selected(obj, domain, ids):
    obj[SEL[domain]] = sorted(ids)


def edit_mask(context, obj, action):
    bm = bmesh.from_edit_mesh(obj.data)
    cfg = context.scene.pmh_settings
    active_domains = [d for d, enabled_flag in zip(("POINT", "EDGE", "FACE"),
                                                   context.tool_settings.mesh_select_mode)
                      if enabled_flag]
    if not active_domains:
        active_domains = [cfg.select_domain]
    layers = {}
    sequences = {"POINT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}
    for d, seq in sequences.items():
        seq.ensure_lookup_table()
        seq.index_update()
        layers[d] = seq.layers.int.get(MASK[d])
        if layers[d] is None:
            layers[d] = seq.layers.int.new(MASK[d])

    if action == 'SHOW':
        for d, seq in sequences.items():
            for elem in seq:
                elem[layers[d]] = 0
                elem.hide_set(False)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        return

    if action == 'ISOLATE':
        for d in sequences:
            if d not in active_domains:
                for elem in sequences[d]:
                    elem[layers[d]] = 0
    for d in active_domains:
        seq = sequences[d]
        selected = {i for i, elem in enumerate(seq) if elem.select and not elem.hide}
        if action in {'HIDE', 'ISOLATE'} and not selected:
            continue
        for i, elem in enumerate(seq):
            if action == 'HIDE' and i in selected:
                elem[layers[d]] = 1
            elif action == 'ISOLATE':
                elem[layers[d]] = int(i not in selected)
            elif action == 'INVERT':
                elem[layers[d]] = 0 if elem[layers[d]] else 1
    # Native edit visibility hides the original editable overlay, not just the
    # Geometry Nodes evaluated display. Does not change the original topology.
    for d, seq in sequences.items():
        for elem in seq:
            elem.hide_set(bool(elem[layers[d]]))
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)


def object_mask(context, obj, action):
    domain = context.scene.pmh_settings.select_domain
    mesh = obj.data
    if action != 'SHOW':
        selected = read_selected(context, obj, domain)
        count = {"POINT": len(mesh.vertices),
                 "EDGE": len(mesh.edges),
                 "FACE": len(mesh.polygons)}[domain]
        selected = {i for i in selected if 0 <= i < count}
        if action in {'HIDE', 'ISOLATE'} and not selected:
            raise RuntimeError("No geometry selected in the PoseMesh picker")
    for d, name in MASK.items():
        attr = mesh.attributes.get(name)
        if attr is None:
            continue
        if action == 'SHOW':
            for item in attr.data:
                item.value = 0
        elif action == 'ISOLATE':
            if d == domain:
                for i, item in enumerate(attr.data):
                    item.value = int(i not in selected)
            else:
                for item in attr.data:
                    item.value = 0
        elif d == domain and action == 'HIDE':
            for i in selected:
                attr.data[i].value = 1
        elif d == domain and action == 'INVERT':
            for item in attr.data:
                item.value = int(not bool(item.value))
    mesh.update()


class PMH_OT_visibility(bpy.types.Operator):
    """Change additive, non-destructive visibility of selected mesh elements"""
    bl_idname = "pmh.visibility"
    bl_label = "PoseMesh Visibility"
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[
        ('HIDE', "Hide selected", "Accumulate hidden elements"),
        ('ISOLATE', "Isolate", "Show only selected elements of this domain"),
        ('INVERT', "Invert", "Invert hidden elements in the chosen domain"),
        ('SHOW', "Reveal all", "Clear all PoseMesh hidden elements")])

    @classmethod
    def poll(cls, context):
        return bool(getattr(context, 'scene', None) and
                    hasattr(context.scene, 'pmh_settings') and
                    enabled(context.scene.pmh_settings, current_mode(context)))

    def execute(self, context):
        obj = choose_target(context)
        try:
            ensure_setup(context, obj)
            if context.mode == 'EDIT_MESH' and obj.mode == 'EDIT':
                edit_mask(context, obj, self.action)
            else:
                object_mask(context, obj, self.action)
            sync_visibility(context)
        except (RuntimeError, ValueError) as e:
            self.report({'WARNING'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


# --------------------------- Viewport geometry picker ------------------------

def get_evaluated(obj, context):
    deps = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(deps)
    me = ev.to_mesh()
    if me is None:
        raise RuntimeError("Evaluated mesh is unavailable")
    return ev, me


def get_id_map(me, domain):
    attr = me.attributes.get(ORIG[domain])
    num = {"POINT": len(me.vertices), "EDGE": len(me.edges),
           "FACE": len(me.polygons)}[domain]
    if attr and len(attr.data) == num:
        return [int(a.value) for a in attr.data]
    return list(range(num))


def get_world_positions(obj, me):
    matrix = obj.matrix_world
    return [matrix @ v.co for v in me.vertices]

def viewport_region(context):
    """The actual viewport region, even when invoked from the N sidebar."""
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return None, None
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    space = area.spaces.active
    rv3d = space.region_3d if space and space.type == 'VIEW_3D' else None
    return region, rv3d


def viewport_mouse(context, event):
    region, _rv3d = viewport_region(context)
    if region is None:
        return None
    return event.mouse_x - region.x, event.mouse_y - region.y




def point_segment_distance(p, a, b):
    ab = b - a
    denom = ab.length_squared
    if denom < 1e-9:
        return (p - a).length
    t = max(0., min(1., (p - a).dot(ab) / denom))
    return (p - (a + ab * t)).length


def point_in_poly_2d(p, polygon):
    # Standard even-odd point-in-polygon test for selection on projected faces.
    inside = False
    if len(polygon) < 3:
        return False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        a, b = polygon[i], polygon[j]
        if ((a.y > p.y) != (b.y > p.y) and
                p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x):
            inside = not inside
        j = i
    return inside


def pick_element(context, obj, domain, px, py, through):
    region, rv3d = viewport_region(context)
    if not region or not rv3d:
        return None
    mouse = Vector((px, py))
    ev, me = get_evaluated(obj, context)
    try:
        if not me.vertices:
            return None
        positions = get_world_positions(obj, me)
        projected = [view3d_utils.location_3d_to_region_2d(region, rv3d, v) for v in positions]
        inv = obj.matrix_world.inverted_safe()
        ray_o = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
        ray_d = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
        local_o = inv @ ray_o
        local_d = (inv.to_3x3() @ ray_d).normalized()
        hit, location, normal, face_index = ev.ray_cast(local_o, local_d)
        face_id = face_index if hit else -1
        mapped = get_id_map(me, domain)

        if domain == 'FACE':
            if through:
                # Through-selection resolves overlapping polygons by proximity
                # to projected face center, rather than only ray front surface.
                best_i, best_score = None, float('inf')
                for i, face in enumerate(me.polygons):
                    poly = [projected[v] for v in face.vertices]
                    if any(p is None for p in poly) or not point_in_poly_2d(mouse, poly):
                        continue
                    center = view3d_utils.location_3d_to_region_2d(
                        region, rv3d, obj.matrix_world @ face.center)
                    if center is None:
                        continue
                    score = (center - mouse).length_squared
                    if score < best_score:
                        best_score, best_i = score, i
                if best_i is not None:
                    return mapped[best_i]
            if face_id < 0 or face_id >= len(mapped):
                return None
            return mapped[face_id]

        max_px = 13.0 if domain == 'POINT' else 11.0
        best_dist = max_px
        best_id = None
        if domain == 'POINT':
            candidates = range(len(me.vertices))
        else:
            candidates = range(len(me.edges))
        # In solid mode, limit candidates to the polygon immediately under the
        # pointer and adjacent silhouettes; through mode can pick all elements.
        nearby_points = None
        if not through and face_id >= 0:
            seed = set(me.polygons[face_id].vertices)
            nearby_points = set(seed)
            # Include exactly one ring; do not recursively traverse the mesh.
            for ed in me.edges:
                if ed.vertices[0] in seed or ed.vertices[1] in seed:
                    nearby_points.update(ed.vertices)
        for i in candidates:
            if domain == 'POINT':
                if nearby_points is not None and i not in nearby_points:
                    continue
                p2 = projected[i]
                if p2 is None:
                    continue
                dist = (p2 - mouse).length
            else:
                ed = me.edges[i]
                a, b = ed.vertices
                if nearby_points is not None and a not in nearby_points and b not in nearby_points:
                    continue
                if projected[a] is None or projected[b] is None:
                    continue
                dist = point_segment_distance(mouse, projected[a], projected[b])
            if dist < best_dist:
                best_dist = dist
                best_id = i
        if best_id is not None and best_id < len(mapped):
            return mapped[best_id]
        return None
    finally:
        ev.to_mesh_clear()


def box_elements(context, obj, domain, rect, through):
    region, rv3d = viewport_region(context)
    if not region or not rv3d:
        return set()
    xmin, ymin, xmax, ymax = rect
    ev, me = get_evaluated(obj, context)
    ids = set()
    try:
        positions = get_world_positions(obj, me)
        projected = [view3d_utils.location_3d_to_region_2d(region, rv3d, v) for v in positions]
        mapped = get_id_map(me, domain)
        def inside(v):
            return v is not None and xmin <= v.x <= xmax and ymin <= v.y <= ymax
        if domain == 'POINT':
            for i, p in enumerate(projected):
                if inside(p):
                    ids.add(mapped[i])
        elif domain == 'EDGE':
            for i, edge in enumerate(me.edges):
                a, b = edge.vertices
                if inside(projected[a]) and inside(projected[b]):
                    ids.add(mapped[i])
        else:
            for i, face in enumerate(me.polygons):
                # The centre test corresponds to face-selection box behavior.
                p = view3d_utils.location_3d_to_region_2d(
                    region, rv3d, obj.matrix_world @ face.center)
                if inside(p):
                    ids.add(mapped[i])
        # X-Ray controls whether selection is through, not box behaviour alone.
        if through:
            return ids
        # Cull rear-facing/occluded components based on ray depth at centre.
        retained = set()
        inv = obj.matrix_world.inverted_safe()
        for i, elem in enumerate({"POINT": me.vertices, "EDGE": me.edges,
                                  "FACE": me.polygons}[domain]):
            if i >= len(mapped) or mapped[i] not in ids:
                continue
            if domain == 'POINT':
                mid = positions[i]
            elif domain == 'EDGE':
                a, b = elem.vertices
                mid = (positions[a] + positions[b]) * .5
            else:
                mid = obj.matrix_world @ elem.center
            screen = view3d_utils.location_3d_to_region_2d(region, rv3d, mid)
            if screen is None:
                continue
            ro = view3d_utils.region_2d_to_origin_3d(region, rv3d, screen)
            rd = view3d_utils.region_2d_to_vector_3d(region, rv3d, screen)
            hit, loc, nor, idx = ev.ray_cast(inv @ ro, (inv.to_3x3() @ rd).normalized())
            if not hit or ((obj.matrix_world @ loc) - ro).length >= (mid - ro).length - .02:
                retained.add(mapped[i])
        return retained
    finally:
        ev.to_mesh_clear()


def draw_box_overlay(operator):
    if not operator._live or not operator._box_origin or not operator._box_current:
        return
    x0, y0 = operator._box_origin
    x1, y1 = operator._box_current
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    shader.uniform_float('color', (.95, .75, .1, .9))
    gpu.state.blend_set('ALPHA')
    vertices = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    batch_for_shader(shader, 'LINE_STRIP', {'pos': vertices}).draw(shader)
    gpu.state.blend_set('NONE')


def draw_picker_overlay(operator):
    context = bpy.context
    if not operator._live or context.area != operator._area:
        return
    obj = operator._object
    if obj is None or obj.name not in bpy.data.objects:
        return
    cfg = context.scene.pmh_settings
    domain = cfg.select_domain
    selected = read_selected(context, obj, domain)
    if not selected:
        return
    try:
        ev, me = get_evaluated(obj, context)
    except Exception:
        return
    try:
        matrix = obj.matrix_world
        points = [matrix @ v.co for v in me.vertices]
        indexmap = get_id_map(me, domain)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        shader.bind()
        shader.uniform_float('color', (.96, .58, .07, .95))
        if domain == 'POINT':
            coords = [points[i] for i, idx in enumerate(indexmap) if idx in selected]
            if coords:
                gpu.state.point_size_set(8.0)
                batch_for_shader(shader, 'POINTS', {'pos': coords}).draw(shader)
        else:
            coords = []
            if domain == 'EDGE':
                for i, ed in enumerate(me.edges):
                    if indexmap[i] in selected:
                        coords.extend((points[ed.vertices[0]], points[ed.vertices[1]]))
            else:
                for i, face in enumerate(me.polygons):
                    if indexmap[i] in selected:
                        verts = list(face.vertices)
                        for j, a in enumerate(verts):
                            coords.extend((points[a], points[verts[(j + 1) % len(verts)]]))
            if coords:
                gpu.state.line_width_set(3.0)
                batch_for_shader(shader, 'LINES', {'pos': coords}).draw(shader)
    finally:
        ev.to_mesh_clear()
        gpu.state.point_size_set(1)
        gpu.state.line_width_set(1)
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('NONE')


class PMH_OT_picker(bpy.types.Operator):
    """Select geometry inside Pose/Object Mode. Click, Shift-add, Ctrl-remove, B-box"""
    bl_idname = 'pmh.picker'
    bl_label = 'PoseMesh Component Picker'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return (context.area and context.area.type == 'VIEW_3D' and
                current_mode(context) in {'POSE', 'OBJECT'} and
                enabled(context.scene.pmh_settings, current_mode(context)))

    def invoke(self, context, event):
        global _active_pick
        if _active_pick is not None:
            self.report({'WARNING'}, 'A PoseMesh picker is already running')
            return {'CANCELLED'}
        self._object = choose_target(context)
        if self._object is None:
            self.report({'WARNING'}, 'Select the target Mesh in the PoseMesh panel')
            return {'CANCELLED'}
        try:
            ensure_setup(context, self._object)
            sync_visibility(context)
        except RuntimeError as e:
            self.report({'WARNING'}, str(e))
            return {'CANCELLED'}
        self._area = context.area
        self._live = True
        self._h_pending = None
        self._timer = context.window_manager.event_timer_add(.05, window=context.window)
        self._box = False
        self._box_origin = None
        self._add = False
        self._subtract = False
        self._box_current = None
        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            draw_picker_overlay, (self,), 'WINDOW', 'POST_VIEW')
        self._box_handler = bpy.types.SpaceView3D.draw_handler_add(
            draw_box_overlay, (self,), 'WINDOW', 'POST_PIXEL')
        _active_pick = self
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('CROSSHAIR')
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def finish(self, context):
        global _active_pick
        self._live = False
        if getattr(self, '_handler', None):
            bpy.types.SpaceView3D.draw_handler_remove(self._handler, 'WINDOW')
            self._handler = None
        if getattr(self, '_box_handler', None):
            bpy.types.SpaceView3D.draw_handler_remove(self._box_handler, 'WINDOW')
            self._box_handler = None
        if getattr(self, '_timer', None):
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        context.window.cursor_modal_restore()
        self._area.tag_redraw()
        _active_pick = None

    def modal(self, context, event):
        if (context.area != self._area or self._object is None or
                current_mode(context) not in {'POSE', 'OBJECT'}):
            self.finish(context)
            return {'CANCELLED'}
        cfg = context.scene.pmh_settings
        if event.type in {'LEFTMOUSE', 'MOUSEMOVE', 'RIGHTMOUSE'}:
            # Sidebar buttons must remain usable while the selector is active.
            # Coordinates are relative to the invoking WINDOW region.
            region = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if (region and (event.mouse_x < region.x or
                    event.mouse_x >= region.x + region.width or
                    event.mouse_y < region.y or
                    event.mouse_y >= region.y + region.height)):
                return {'RUNNING_MODAL', 'PASS_THROUGH'}
        if event.type == 'TIMER':
            if self._h_pending and time.monotonic() - self._h_pending[1] >= TAP_TIMEOUT:
                native_single(context, self._h_pending[0])
                self._h_pending = None
            return {'RUNNING_MODAL'}
        if event.type == 'H' and event.value in {'PRESS', 'DOUBLE_CLICK'} and not getattr(event, 'is_repeat', False):
            variant = ('SHOW' if event.alt and not event.ctrl and not event.shift else
                       'CTRL' if event.ctrl and not event.alt and not event.shift else
                       'ISOLATE' if event.shift and not event.ctrl and not event.alt else
                       'HIDE' if not (event.alt or event.ctrl or event.shift) else None)
            if variant:
                now = time.monotonic()
                if (self._h_pending and self._h_pending[0] == variant and
                        now - self._h_pending[1] <= TAP_TIMEOUT):
                    self._h_pending = None
                    bpy.ops.pmh.visibility('EXEC_DEFAULT', action=('INVERT' if variant == 'CTRL' else variant))
                    context.area.tag_redraw()
                else:
                    if self._h_pending:
                        native_single(context, self._h_pending[0])
                    self._h_pending = (variant, now)
                return {'RUNNING_MODAL'}
        if event.type == 'H' and event.value == 'RELEASE':
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.finish(context)
            return {'FINISHED'}
        if event.type in {'ONE', 'TWO', 'THREE'} and event.value == 'PRESS':
            cfg.select_domain = {'ONE': 'POINT', 'TWO': 'EDGE', 'THREE': 'FACE'}[event.type]
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'A' and event.value == 'PRESS':
            domain = cfg.select_domain
            mesh = self._object.data
            n = {"POINT": len(mesh.vertices), "EDGE": len(mesh.edges),
                 "FACE": len(mesh.polygons)}[domain]
            hidden = mesh.attributes.get(MASK[domain])
            if event.alt:
                write_selected(self._object, domain, set())
            else:
                write_selected(self._object, domain,
                               {i for i in range(n) if not hidden or not hidden.data[i].value})
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'B' and event.value == 'PRESS':
            self._box = True
            self._box_origin = None
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if self._box:
                self._box_origin = viewport_mouse(context, event)
                self._box_current = self._box_origin
                self._add = event.shift
                self._subtract = event.ctrl
                return {'RUNNING_MODAL'}
            px, py = viewport_mouse(context, event)
            elem = pick_element(context, self._object, cfg.select_domain,
                                px, py, cfg.pick_through)
            selection = read_selected(context, self._object, cfg.select_domain)
            if elem is None:
                if not event.shift and not event.ctrl:
                    selection.clear()
            elif event.ctrl:
                selection.discard(elem)
            elif event.shift:
                if elem in selection:
                    selection.remove(elem)
                else:
                    selection.add(elem)
            else:
                selection = {elem}
            write_selected(self._object, cfg.select_domain, selection)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if self._box and self._box_origin and event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            x0, y0 = self._box_origin
            x1, y1 = viewport_mouse(context, event)
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            found = box_elements(context, self._object, cfg.select_domain, rect, cfg.pick_through)
            selected = read_selected(context, self._object, cfg.select_domain)
            if self._subtract:
                selected.difference_update(found)
            elif self._add:
                selected.update(found)
            else:
                selected = found
            write_selected(self._object, cfg.select_domain, selected)
            self._box = False
            self._box_origin = None
            self._box_current = None
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if self._box_origin and event.type == 'MOUSEMOVE':
            self._box_current = viewport_mouse(context, event)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEMOVE'}:
            return {'RUNNING_MODAL', 'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


# ---------------------- Double-tap keys with native fallback -----------------


def native_single(context, action):
    """Keep Blender's ordinary H/Shift-H/Alt-H actions on a single press."""
    mode = current_mode(context)
    try:
        if action == 'CTRL':
            # Ctrl-H is context-specific (e.g. Hooks menu); replay the active
            # keymap item instead of guessing the operator for the current mode.
            keyconf = context.window_manager.keyconfigs.active
            names = {'POSE': ('Pose', '3D View'),
                     'EDIT': ('Mesh', '3D View'),
                     'OBJECT': ('Object Mode', '3D View')}.get(mode, ())
            if keyconf:
                for kmname in names:
                    km = keyconf.keymaps.get(kmname)
                    if not km:
                        continue
                    for item in km.keymap_items:
                        if (not item.active or item.type != 'H' or item.value != 'PRESS'
                                or not item.ctrl or item.shift or item.alt
                                or item.idname.startswith('pmh.')):
                            continue
                        try:
                            group, name = item.idname.split('.', 1)
                            func = getattr(getattr(bpy.ops, group), name)
                            kwargs = {}
                            if item.properties:
                                for prop in item.properties.bl_rna.properties:
                                    if prop.identifier != 'rna_type' and not prop.is_readonly:
                                        try:
                                            kwargs[prop.identifier] = getattr(item.properties, prop.identifier)
                                        except (AttributeError, ValueError):
                                            pass
                            func('INVOKE_DEFAULT', **kwargs)
                            return
                        except Exception:
                            continue
            return
        if mode == 'POSE':
            if action == 'SHOW':
                bpy.ops.pose.reveal()
            else:
                bpy.ops.pose.hide(unselected=(action == 'ISOLATE'))
        elif mode == 'EDIT':
            if action == 'SHOW':
                bpy.ops.mesh.reveal()
            else:
                bpy.ops.mesh.hide(unselected=(action == 'ISOLATE'))
        elif mode == 'OBJECT':
            if action == 'SHOW':
                bpy.ops.object.hide_view_clear()
            else:
                bpy.ops.object.hide_view_set(unselected=(action == 'ISOLATE'))
    except (RuntimeError, AttributeError):
        pass


class PMH_OT_tap(bpy.types.Operator):
    """Detect H H within 0.33 seconds; defer native single H until timeout"""
    bl_idname = 'pmh.double_h'
    bl_label = 'PoseMesh Double-H Shortcut'
    bl_options = {'INTERNAL'}

    action: EnumProperty(items=[
        ('HIDE', 'Hide', ''), ('ISOLATE', 'Isolate', ''),
        ('INVERT', 'Invert', ''), ('SHOW', 'Show', ''), ('CTRL', 'Ctrl', '')])

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == 'VIEW_3D' and
                    hasattr(context.scene, 'pmh_settings') and
                    enabled(context.scene.pmh_settings, current_mode(context)))

    def invoke(self, context, event):
        self._shift, self._ctrl, self._alt = event.shift, event.ctrl, event.alt
        self._start = time.monotonic()
        self._wm = context.window_manager
        self._timer = self._wm.event_timer_add(.04, window=context.window)
        self._wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def done(self):
        if getattr(self, '_timer', None):
            self._wm.event_timer_remove(self._timer)
            self._timer = None

    def modal(self, context, event):
        if (event.type == 'H' and event.value in {'PRESS', 'DOUBLE_CLICK'}
                and not getattr(event, 'is_repeat', False)
                and (event.shift, event.ctrl, event.alt) ==
                    (self._shift, self._ctrl, self._alt)):
            self.done()
            action = 'INVERT' if self.action == 'CTRL' else self.action
            bpy.ops.pmh.visibility('EXEC_DEFAULT', action=action)
            return {'FINISHED'}
        if event.type == 'TIMER':
            if time.monotonic() - self._start >= TAP_TIMEOUT:
                self.done()
                native_single(context, self.action)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}
        # Swallow only the release of the initiating H. Let mouse and other
        # shortcuts continue to work instead of leaving a permanent listener.
        if event.type == 'H' and event.value == 'RELEASE':
            return {'RUNNING_MODAL'}
        if event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'} and event.value == 'PRESS':
            self.done()
            native_single(context, self.action)
            return {'FINISHED', 'PASS_THROUGH'}
        return {'RUNNING_MODAL', 'PASS_THROUGH'}


class PMH_OT_debug_event(bpy.types.Operator):
    """Print Event public attributes/methods and RNA metadata once"""
    bl_idname = 'pmh.debug_event'
    bl_label = 'Inspect Event API'
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        print('\n' + '=' * 64)
        print('PoseMesh Hide 0.1.1 - bpy.types.Event introspection')
        print('Actual event:', event.type, event.value)
        print('Public names returned by dir(event):')
        for name in dir(event):
            if name.startswith('_'):
                continue
            try:
                value = getattr(event, name)
                if callable(value):
                    print(f'  METHOD  {name}')
                else:
                    print(f'  FIELD   {name:29} = {value!r}')
            except Exception as exc:
                print(f'  ERROR   {name:29} = {exc!s}')
        print('RNA properties:')
        for prop in event.bl_rna.properties:
            print(f'  {prop.identifier:29} {prop.type:10} '
                  f'readonly={prop.is_readonly}')
        print('=' * 64 + '\n')
        self.report({'INFO'}, 'Event API printed to Blender system console')
        return {'FINISHED'}


class PMH_OT_shading(bpy.types.Operator):
    bl_idname = 'pmh.shading'
    bl_label = 'PoseMesh Shading'
    bl_options = {'INTERNAL'}
    kind: EnumProperty(items=[('XRAY', 'X-Ray', ''), ('WIRE', 'Wireframe', '')])

    def execute(self, context):
        if context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        shading = context.space_data.shading
        if self.kind == 'WIRE':
            shading.type = 'SOLID' if shading.type == 'WIREFRAME' else 'WIREFRAME'
        else:
            shading.xray_alpha = context.scene.pmh_settings.xray_alpha
            if shading.type == 'WIREFRAME':
                shading.show_xray_wireframe = not shading.show_xray_wireframe
                context.scene.pmh_settings.pick_through = shading.show_xray_wireframe
            else:
                shading.show_xray = not shading.show_xray
                context.scene.pmh_settings.pick_through = shading.show_xray
        return {'FINISHED'}


class PMH_PT_panel(bpy.types.Panel):
    bl_label = 'PoseMesh Hide'
    bl_idname = 'PMH_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'PoseMesh'

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.pmh_settings
        layout.prop(cfg, 'target')
        layout.label(text='Enabled modes (OR):')
        row = layout.row(align=True)
        row.prop(cfg, 'affect_object', toggle=True)
        row.prop(cfg, 'affect_pose', toggle=True)
        row.prop(cfg, 'affect_edit', toggle=True)
        mode = current_mode(context)
        layout.label(text=f'Current mode: {mode}')
        enabled_now = enabled(cfg, mode)
        col = layout.column()
        col.enabled = enabled_now
        col.prop(cfg, 'select_domain', expand=True)
        if mode in {'POSE', 'OBJECT'}:
            col.operator('pmh.picker', icon='RESTRICT_SELECT_OFF', text='Select Mesh Components')
            col.label(text='Picker: click / Shift / Ctrl / B')
            col.label(text='1 vertex, 2 edge, 3 face; Esc exit')
        elif mode == 'EDIT':
            col.label(text='Select using Blender native tools')
        else:
            col.label(text='Switch to Pose, Object or Edit')
        row = col.row(align=True)
        op = row.operator('pmh.visibility', text='Hide', icon='HIDE_ON')
        op.action = 'HIDE'
        op = row.operator('pmh.visibility', text='Isolate', icon='SOLO_ON')
        op.action = 'ISOLATE'
        row = col.row(align=True)
        op = row.operator('pmh.visibility', text='Invert', icon='ARROW_LEFTRIGHT')
        op.action = 'INVERT'
        op = row.operator('pmh.visibility', text='Reveal all', icon='HIDE_OFF')
        op.action = 'SHOW'
        layout.separator()
        row = layout.row(align=True)
        row.operator('pmh.shading', text='X-Ray', icon='XRAY').kind = 'XRAY'
        row.operator('pmh.shading', text='Wireframe', icon='SHADING_WIRE').kind = 'WIRE'
        layout.prop(cfg, 'pick_through')
        layout.prop(cfg, 'xray_alpha')
        layout.separator()
        layout.label(text='H H: Hide / Shift H H: Isolate')
        layout.label(text='Ctrl H H: Invert / Alt H H: Reveal')
        layout.label(text='Single H retains Blender action')
        layout.operator('pmh.debug_event', text='Print Event API', icon='CONSOLE')


@persistent
def pmh_load_post(_dummy):
    sync_visibility()


def mode_timer():
    if not hasattr(bpy.types.Scene, 'pmh_settings'):
        return None
    try:
        sync_visibility()
    except (ReferenceError, RuntimeError):
        pass
    return .30


CLASSES = (PMH_Settings, PMH_OT_visibility, PMH_OT_picker,
           PMH_OT_tap, PMH_OT_debug_event, PMH_OT_shading, PMH_PT_panel)


def register_hotkeys():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm else None
    if not kc:
        return
    for kmname, space_type in [('Object Mode', 'EMPTY'),
                               ('Pose', 'EMPTY'), ('Mesh', 'EMPTY')]:
        km = kc.keymaps.new(name=kmname, space_type=space_type)
        for action, shift, ctrl, alt in [
            ('HIDE', False, False, False),
            ('ISOLATE', True, False, False),
            ('CTRL', False, True, False),
            ('SHOW', False, False, True),
        ]:
            kmi = km.keymap_items.new('pmh.double_h', type='H', value='PRESS',
                                     shift=shift, ctrl=ctrl, alt=alt, head=True)
            kmi.properties.action = action
            _keymaps.append((km, kmi))


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pmh_settings = PointerProperty(type=PMH_Settings)
    register_hotkeys()
    if pmh_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(pmh_load_post)
    if not bpy.app.timers.is_registered(mode_timer):
        bpy.app.timers.register(mode_timer, first_interval=.3, persistent=True)


def unregister():
    global _active_pick
    if _active_pick is not None:
        try:
            _active_pick.finish(bpy.context)
        except Exception:
            pass
        _active_pick = None
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except ReferenceError:
            pass
    _keymaps.clear()
    if pmh_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(pmh_load_post)
    if bpy.app.timers.is_registered(mode_timer):
        bpy.app.timers.unregister(mode_timer)
    if hasattr(bpy.types.Scene, 'pmh_settings'):
        del bpy.types.Scene.pmh_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
