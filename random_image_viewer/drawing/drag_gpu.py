"""🌊🧊 Smear and Push on the graphics card (OpenCL).

DragStroke and PushStroke keep the view under the stroke as numpy arrays and
work it a step (a few pixels of pen travel) at a time - each step thousands
to a hundred thousand pixels read and written, ~1 ms in numpy, a few steps
per pen move. Here the arrays stay on the graphics card for the whole
stroke and each step is one or two small kernels; only what the hairs met
(a few hundred colours) comes back per step, and the pixels only where the
stroke is composed for the screen (see fetch).

The kernels do exactly what the numpy code does (same rounding, same
truncation to 8 bits), so a stroke looks the same either way. Anything the
driver refuses switches the stroke back to numpy (see DragStroke._cl_off).
"""

import numpy as np

_KERNELS = r"""
__kernel void drag_met(__global const uchar4* pix, int w, int h,
                       float fx, float fy, float ux, float uy, float vx, float vy,
                       int nss, int half_, __global float4* met)
{
    int j = get_global_id(0);
    float js = (float)(j - 2 * half_) * 0.5f;
    float4 acc = (float4)(0.0f);
    for (int k = 0; k < nss; k++) {
        float ss = (float)k * 0.5f;
        int ix = (int)rint(fx - ux * ss + vx * js);
        int iy = (int)rint(fy - uy * ss + vy * js);
        ix = clamp(ix, 0, w - 1);
        iy = clamp(iy, 0, h - 1);
        acc += convert_float4(pix[iy * w + ix]);
    }
    met[j] = acc / (float)nss;
}

__kernel void drag_lay(__global uchar4* pix, __global uchar* reached, __global float* laid_at,
                       int w, int bx0, int by0,
                       float fx, float fy, float ux, float uy, float vx, float vy,
                       float travelled, float reach_back, int nh, int half_,
                       __global const float4* lay, __global const float* mixing,
                       __global const float* cover, float travel)
{
    int x = bx0 + get_global_id(0);
    int y = by0 + get_global_id(1);
    float gx = (float)x - fx;
    float gy = (float)y - fy;
    float back = -(gx * ux + gy * uy);
    float across = gx * vx + gy * vy;
    int hair = (int)rint(across * 2.0f) + 2 * half_;
    if (back < -0.5f || back >= travelled + reach_back || hair < 0 || hair >= nh)
        return;
    float c = cover[hair];
    if (c <= 0.02f)
        return;
    int i = y * w + x;
    float4 here = convert_float4(pix[i]);
    float4 col = here + (lay[hair] - here) * mixing[hair];
    pix[i] = convert_uchar4_sat_rtz(col + 0.5f);
    uchar shown = convert_uchar_sat_rtz(c * 255.0f);
    reached[i] = max(reached[i], shown);
    laid_at[i] = travel - clamp(back, 0.0f, travelled);
}

__kernel void push_warp(__global uchar4* pix, __global const uchar4* prev,
                        __global uchar* reached, __global float* laid_at,
                        int w, int h, int ax0, int ay0, int x0, int y0, int size,
                        __global const float* grey, __global const uchar* tipa,
                        __global const float* lag, __global const float* stepr,
                        float strength, float ice, float even, float mvx, float mvy,
                        uchar cover, float travel)
{
    int x = ax0 + get_global_id(0);
    int y = ay0 + get_global_id(1);
    int sx = x - x0, sy = y - y0;
    int i = y * w + x;
    float share = grey[sy * size + sx] * strength;
    if (ice > 0.0f)
        share = share * (1.0f - ice * lag[i]);
    float step = (1.0f - ice) * even + ice * stepr[i];
    int mx = (int)floor(mvx * share + step);
    int my = (int)floor(mvy * share + step);
    int srcy = clamp(y - my, 0, h - 1);
    int srcx = clamp(x - mx, 0, w - 1);
    pix[i] = prev[srcy * w + srcx];
    if (tipa[sy * size + sx] > 5) {
        reached[i] = max(reached[i], cover);
        laid_at[i] = travel;
    }
}
"""

_gpu = None             # the app's GPU processor (its OpenCL context and queue)
_program = None
_failed = False


def use_gpu(processor):
    """Smear and Push on *processor* (GPULutProcessor) when it is up."""
    global _gpu
    _gpu = processor


def available():
    return (_gpu is not None and not _failed and getattr(_gpu, 'gpu_enabled', False)
            and getattr(_gpu, 'context', None) is not None)


def _cl():
    import random_image_viewer.constants as constants
    return constants.cl


def prebuild():
    """Build the kernels now (idle, after start-up): ~0.4 s the first big
    smear would otherwise wait for."""
    global _program
    if _program is None and available():
        try:
            _program = _cl().Program(_gpu.context, _KERNELS).build()
        except Exception as e:
            failed(e)


def failed(error):
    """The driver would not: numpy from now on (this session)."""
    global _failed
    if not _failed:
        print(f"🌊 Smear / Push on the GPU failed, using the CPU: {error}")
    _failed = True


class DragGpu:
    """One stroke's arrays on the graphics card: the pixels (h, w, 4 uint8),
    where the tip reached (h, w uint8) and when (h, w float32)."""

    def __init__(self, pixels, reached, laid_at):
        global _program
        cl = _cl()
        self.cl = cl
        self.ctx, self.queue = _gpu.context, _gpu.queue
        if _program is None:
            _program = cl.Program(self.ctx, _KERNELS).build()
        self.prog = _program
        self.k_met = cl.Kernel(_program, 'drag_met')
        self.k_lay = cl.Kernel(_program, 'drag_lay')
        self.k_push = cl.Kernel(_program, 'push_warp')
        self.h, self.w = reached.shape
        mf = cl.mem_flags
        self.pix = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR,
                             hostbuf=np.ascontiguousarray(pixels))
        self.reached = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR,
                                 hostbuf=np.ascontiguousarray(reached))
        self.laid_at = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR,
                                 hostbuf=np.ascontiguousarray(laid_at))
        self.prev = None        # Push: the pixels before the step (it reads them)
        self.extra = {}         # Push: lag / step fields, uploaded once

    def _small(self, array, dtype=np.float32):
        mf = self.cl.mem_flags
        return self.cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                              hostbuf=np.ascontiguousarray(array, dtype=dtype))

    def met(self, fx, fy, ux, uy, vx, vy, nss, half, hairs):
        """What each hair passed over this step, averaged (hairs, 4) float32."""
        out = np.empty((hairs, 4), np.float32)
        buf = self.cl.Buffer(self.ctx, self.cl.mem_flags.WRITE_ONLY, out.nbytes)
        f, i = np.float32, np.int32
        self.k_met(self.queue, (hairs,), None, self.pix, i(self.w), i(self.h),
                   f(fx), f(fy), f(ux), f(uy), f(vx), f(vy), i(nss), i(half), buf)
        self.cl.enqueue_copy(self.queue, out, buf)
        return out

    def lay(self, box, fx, fy, ux, uy, vx, vy, travelled, reach_back, half, lay, mixing, cover, travel):
        bx0, by0, bx1, by1 = box
        f, i = np.float32, np.int32
        self.k_lay(self.queue, (bx1 - bx0, by1 - by0), None, self.pix, self.reached, self.laid_at,
                   i(self.w), i(bx0), i(by0), f(fx), f(fy), f(ux), f(uy), f(vx), f(vy),
                   f(travelled), f(reach_back), i(len(cover)), i(half),
                   self._small(lay), self._small(mixing), self._small(cover), f(travel))

    def push(self, area, x0, y0, grey, tip_a, lag, stepr, strength, ice, even, mvx, mvy, cover, travel):
        cl = self.cl
        if self.prev is None:
            self.prev = cl.Buffer(self.ctx, cl.mem_flags.READ_WRITE, self.w * self.h * 4)
        if 'lag' not in self.extra:
            self.extra['lag'] = self._small(lag)
            self.extra['step'] = self._small(stepr)
        cl.enqueue_copy(self.queue, self.prev, self.pix)        # read before written
        ax0, ay0, ax1, ay1 = area
        f, i = np.float32, np.int32
        self.k_push(self.queue, (ax1 - ax0, ay1 - ay0), None, self.pix, self.prev,
                    self.reached, self.laid_at, i(self.w), i(self.h), i(ax0), i(ay0),
                    i(x0), i(y0), i(grey.shape[0]),
                    self._small(grey), self._small(tip_a, np.uint8),
                    self.extra['lag'], self.extra['step'],
                    f(strength), f(ice), f(even), f(mvx), f(mvy), np.uint8(cover), f(travel))

    def fetch(self, x0, y0, x1, y1, pixels, reached, laid_at):
        """The arrays' (x0, y0)-(x1, y1) part back into the numpy ones."""
        if x1 <= x0 or y1 <= y0:
            return
        cl, q, w = self.cl, self.queue, self.w
        rw, rh = x1 - x0, y1 - y0
        for buf, host, bpp in ((self.pix, pixels, 4), (self.reached, reached, 1), (self.laid_at, laid_at, 4)):
            tmp = np.empty((rh, rw * bpp), np.uint8)
            cl.enqueue_copy(q, tmp, buf, buffer_origin=(x0 * bpp, y0, 0), host_origin=(0, 0, 0),
                            region=(rw * bpp, rh, 1), buffer_pitches=(w * bpp, 0),
                            host_pitches=(rw * bpp, 0))
            if host.dtype == np.float32:
                host[y0:y1, x0:x1] = tmp.view(np.float32).reshape(rh, rw)
            elif host.ndim == 3:
                host[y0:y1, x0:x1] = tmp.reshape(rh, rw, 4)
            else:
                host[y0:y1, x0:x1] = tmp
        q.finish()
