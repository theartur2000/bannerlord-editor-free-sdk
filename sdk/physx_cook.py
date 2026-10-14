"""Cooking PhysX convex hulls and triangle meshes with the game's own PhysX 4.1.2 cooking library (ctypes, no compiled helper).

The game ships PhysXCooking_64.dll (exports PxCreateCooking) with PhysXFoundation_64.dll (PxCreateFoundation) and PhysXCommon_64.dll in
bin/Win64_Shipping_Client; this module loads them into the Python process (it never runs a game program and never writes into the game
folder). The binding is the 4.1 public interface written in Python: the C++ objects PhysX calls back (PxAllocatorCallback, PxErrorCallback,
PxOutputStream) are a pointer to a table of ctypes function pointers in the headers' virtual method order (MSVC x64: one slot per virtual
destructor), and PxCooking's methods are called through its own table (0 release, 1 setParams, 2 getParams, 3 platformMismatch,
4 cookTriangleMesh, 5 createTriangleMesh, 6 validateTriangleMesh, 7 cookConvexMesh, ...). The structs below are the 4.1 headers' field lists
(PxCookingParams 48 bytes, PxConvexMeshDesc 80, PxTriangleMeshDesc 72, asserted).

The parameters that reproduce the game's stored cooks were found by cooking every manifold of every shape the game ships and comparing bytes
(physics.py has the counts): PxCookingParams at its 4.1 defaults (scale 1 / 10, area test 0.06, plane tolerance 0.0007, no mesh
pre-processing, no welding, remap table kept, no adjacency, no GPU data, Gauss map limit 32), the BVH33 mid-phase at its defaults (0.55,
eSIM_PERFORMANCE) or BVH34 with 4 triangles per leaf; hulls with eCOMPUTE_CONVEX only, vertex limit 255, and when that cook reports
ePOLYGONS_LIMIT_REACHED the engine cooks again with eQUANTIZE_INPUT added and stores both results one after the other in the same blob.
"""
import ctypes as C, os, struct, threading

import numpy as np

if __package__ in (None, ""):
    import config
else:
    from . import config

BIN = os.path.join(config.GAME, "bin", "Win64_Shipping_Client")
PX_VERSION = (4 << 24) + (1 << 16) + (2 << 8)          # PX_PHYSICS_VERSION 4.1.2, also what PxCreateFoundation checks in 4.1

# PxConvexFlag
COMPUTE_CONVEX, CHECK_ZERO_AREA, QUANTIZE_INPUT = 1 << 1, 1 << 2, 1 << 3
# PxConvexMeshCookingResult / PxTriangleMeshCookingResult
CONVEX_RESULT = {0: "success", 1: "zero area test failed", 2: "polygons limit reached", 3: "failure"}
MESH_RESULT = {0: "success", 1: "large triangle", 2: "failure"}
MIDPHASE = {"bvh33": 0, "bvh34": 1}                    # PxMeshMidPhase; the game's meshes: "RTRE" tree = BVH33, "BV4 " = BVH34

VP = C.c_void_p


class Tolerances(C.Structure): _fields_ = [("length", C.c_float), ("speed", C.c_float)]
class Midphase(C.Structure): _fields_ = [("u0", C.c_uint32), ("u1", C.c_uint32), ("type", C.c_int)]   # union {BVH33: f32 tradeoff, enum hint; BVH34: u32 prims per leaf}, mType
class CookingParams(C.Structure):
    _fields_ = [("areaTestEpsilon", C.c_float), ("planeTolerance", C.c_float), ("convexMeshCookingType", C.c_int), ("suppressTriangleMeshRemapTable", C.c_bool),
                ("buildTriangleAdjacencies", C.c_bool), ("buildGPUData", C.c_bool), ("scale", Tolerances), ("meshPreprocessParams", C.c_uint32),
                ("meshWeldTolerance", C.c_float), ("midphaseDesc", Midphase), ("gaussMapLimit", C.c_uint32)]
class BoundedData(C.Structure): _fields_ = [("stride", C.c_uint32), ("data", VP), ("count", C.c_uint32)]
class StridedData(C.Structure): _fields_ = [("stride", C.c_uint32), ("data", VP)]
class ConvexMeshDesc(C.Structure):
    _fields_ = [("points", BoundedData), ("polygons", BoundedData), ("indices", BoundedData), ("flags", C.c_uint16), ("vertexLimit", C.c_uint16), ("quantizedCount", C.c_uint16)]
class TriangleMeshDesc(C.Structure):     # PxSimpleTriangleMesh (points, triangles, u16 flags) + materialIndices
    _fields_ = [("points", BoundedData), ("triangles", BoundedData), ("flags", C.c_uint16), ("materialIndices", StridedData)]
assert (C.sizeof(CookingParams), C.sizeof(ConvexMeshDesc), C.sizeof(TriangleMeshDesc)) == (48, 80, 72)

DTOR = C.CFUNCTYPE(None, VP, C.c_uint)                 # the scalar deleting destructor slot; PhysX never deletes our objects
ALLOC = C.CFUNCTYPE(VP, VP, C.c_size_t, C.c_char_p, C.c_char_p, C.c_int)
FREE = C.CFUNCTYPE(None, VP, VP)
REPORT = C.CFUNCTYPE(None, VP, C.c_int, C.c_char_p, C.c_char_p, C.c_int)
WRITE = C.CFUNCTYPE(C.c_uint32, VP, VP, C.c_uint32)
COOK_MESH = C.CFUNCTYPE(C.c_bool, VP, C.POINTER(TriangleMeshDesc), VP, C.POINTER(C.c_int))
COOK_CONVEX = C.CFUNCTYPE(C.c_bool, VP, C.POINTER(ConvexMeshDesc), VP, C.POINTER(C.c_int))
SET_PARAMS = C.CFUNCTYPE(None, VP, C.POINTER(CookingParams))


def _object(funcs, keep):
    """A C++ object for PhysX to call: its first 8 bytes point at the table of the given function pointers."""
    table = (VP * len(funcs))(*[C.cast(f, VP) for f in funcs]); obj = (VP * 1)(C.cast(table, VP)); keep += [funcs, table, obj]; return obj


def params(midphase="bvh33"):
    """PxCookingParams(PxTolerancesScale()) with the given mid-phase at its defaults: the settings of the game's cooks."""
    if midphase not in MIDPHASE: raise ValueError("midphase is 'bvh33' or 'bvh34'")
    mid = Midphase(struct.unpack("<I", struct.pack("<f", 0.55))[0], 0, 0) if midphase == "bvh33" else Midphase(4, 0, 1)
    return CookingParams(0.06, 0.0007, 0, False, False, False, Tolerances(1.0, 10.0), 0, 0.0, mid, 32)


class Cooker:
    """One foundation and one PxCooking for the process (PhysX allows one foundation per process); calls are serialised by a lock."""
    def __init__(s, bin_dir=BIN):
        s.keep = []; s.errors = []; s.lock = threading.Lock()
        for n in ("PhysXFoundation_64.dll", "PhysXCommon_64.dll", "PhysXCooking_64.dll"):
            if not os.path.isfile(os.path.join(bin_dir, n)): raise OSError("%s not found in %s" % (n, bin_dir))
        s._dirs = os.add_dll_directory(bin_dir)
        fnd = C.CDLL(os.path.join(bin_dir, "PhysXFoundation_64.dll")); C.CDLL(os.path.join(bin_dir, "PhysXCommon_64.dll")); ck = C.CDLL(os.path.join(bin_dir, "PhysXCooking_64.dll"))
        crt = C.CDLL("ucrtbase"); crt._aligned_malloc.restype = VP; crt._aligned_malloc.argtypes = [C.c_size_t, C.c_size_t]; crt._aligned_free.argtypes = [VP]
        # PhysX requires 16-byte aligned allocations
        alloc = _object([DTOR(lambda o, f: None), ALLOC(lambda o, n, t, fn, ln: crt._aligned_malloc(max(n, 1), 16)), FREE(lambda o, p: crt._aligned_free(p) if p else None)], s.keep)
        err = _object([DTOR(lambda o, f: None), REPORT(lambda o, code, msg, fn, ln: s.errors.append("%d %s" % (code, (msg or b"").decode("utf-8", "replace"))))], s.keep)
        fnd.PxCreateFoundation.restype = VP; fnd.PxCreateFoundation.argtypes = [C.c_uint, VP, VP]
        fnd.PxGetFoundation.restype = VP
        f = fnd.PxCreateFoundation(PX_VERSION, C.cast(alloc, VP), C.cast(err, VP))
        if not f: raise OSError("PxCreateFoundation failed (another PhysX foundation in this process?) %s" % s.errors)
        ck.PxCreateCooking.restype = VP; ck.PxCreateCooking.argtypes = [C.c_uint, VP, C.POINTER(CookingParams)]
        s.params = params(); s.K = ck.PxCreateCooking(PX_VERSION, f, C.byref(s.params))
        if not s.K: raise OSError("PxCreateCooking failed %s" % s.errors)
        vt = C.cast(C.cast(s.K, C.POINTER(VP))[0], C.POINTER(VP))
        s._set, s._mesh, s._convex = SET_PARAMS(vt[1]), COOK_MESH(vt[4]), COOK_CONVEX(vt[7]); s.midphase = "bvh33"

    def _stream(s):
        buf = bytearray()
        w = WRITE(lambda o, src, n: (buf.extend(C.string_at(src, n)), n)[1])
        keep = []; return buf, _object([w, DTOR(lambda o, f: None)], keep), keep          # PxOutputStream: write first, then the destructor

    def _midphase(s, m):
        if m != s.midphase: s.params = params(m); s._set(s.K, C.byref(s.params)); s.midphase = m

    def convex(s, points, flags=COMPUTE_CONVEX, vertex_limit=255, quantized_count=255, stream=None):
        """(cooked bytes or None, result code) of one cookConvexMesh call; with stream=(buf, obj, keep) the bytes are appended to it."""
        P = np.ascontiguousarray(np.asarray(points, "<f4").reshape(-1, 3)); d = ConvexMeshDesc(); d.points = BoundedData(12, P.ctypes.data, len(P))
        d.flags, d.vertexLimit, d.quantizedCount = flags, vertex_limit, quantized_count
        buf, obj, keep = stream or s._stream(); r = C.c_int(-1); ok = s._convex(s.K, C.byref(d), C.cast(obj, VP), C.byref(r))
        return (bytes(buf) if ok else None), r.value

    def hull(s, points):
        """A convex hull cooked as the engine cooks it (see the module docstring): bytes ("NXS" 1 "CVXM" ..., one or two cooked hulls)."""
        with s.lock:
            s.errors.clear(); st = s._stream()
            b, r = s.convex(points, COMPUTE_CONVEX, stream=st)
            if b is not None and r == 2: b, r = s.convex(points, COMPUTE_CONVEX | QUANTIZE_INPUT, stream=st)
            if b is None: raise ValueError("PhysX could not cook the hull (%s) %s" % (CONVEX_RESULT.get(r, r), s.errors[-3:]))
            return b

    def mesh(s, vertices, triangles, material_index=None, midphase="bvh33"):
        """A triangle mesh cooked as the engine cooks it: bytes ("NXS" 1 "MESH" ...). material_index: u16 per triangle (None: no table)."""
        P = np.ascontiguousarray(np.asarray(vertices, "<f4").reshape(-1, 3)); T = np.ascontiguousarray(np.asarray(triangles, "<u4").reshape(-1, 3))
        d = TriangleMeshDesc(); d.points = BoundedData(12, P.ctypes.data, len(P)); d.triangles = BoundedData(12, T.ctypes.data, len(T))
        if material_index is not None:
            M = np.ascontiguousarray(np.asarray(material_index, "<u2").reshape(len(T))); d.materialIndices = StridedData(2, M.ctypes.data)
        with s.lock:
            s.errors.clear(); s._midphase(midphase); buf, obj, keep = s._stream(); r = C.c_int(-1)
            if not s._mesh(s.K, C.byref(d), C.cast(obj, VP), C.byref(r)): raise ValueError("PhysX could not cook the mesh (%s) %s" % (MESH_RESULT.get(r.value, r.value), s.errors[-3:]))
            return bytes(buf)


_COOKER = None; _WHY = None

def cooker():
    """The process's Cooker (created on first use). Raises OSError when the game's PhysX libraries cannot be loaded."""
    global _COOKER, _WHY
    if _COOKER is None:
        if _WHY: raise OSError(_WHY)
        try: _COOKER = Cooker()
        except (OSError, AttributeError) as e: _WHY = "PhysX cooking not available: %s" % e; raise OSError(_WHY)
    return _COOKER

def available():
    """True when the game's PhysX cooking library loads in this process (Windows, game installed)."""
    try: cooker(); return True
    except OSError: return False

def why_not():
    return None if available() else _WHY

def hull(points): return cooker().hull(points)

def mesh(vertices, triangles, material_index=None, midphase="bvh33"): return cooker().mesh(vertices, triangles, material_index, midphase)
