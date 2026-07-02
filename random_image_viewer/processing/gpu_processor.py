from PySide6.QtGui import QPixmap, QImage

from random_image_viewer.constants import cl, GPU_AVAILABLE, _check_gpu_available
import random_image_viewer.constants as _constants


class GPULutProcessor:
    """GPU-accelerated LUT processing using OpenCL"""

    def __init__(self):
        self.context = None
        self.queue = None
        self.program = None
        self.device = None
        self.gpu_enabled = False
        self.max_buffer_size = 0
        self._force_reinit = False
        self._initialized = False  # Lazy init flag

        # Cache kernels to avoid repeated retrieval
        self._kernel_cache = {}
        # Do NOT call _initialize_gpu() here - initialization is deferred to first use

    def _initialize_gpu(self):
        """Initialize OpenCL context and compile kernels (lazy - called on first use)"""
        if _constants.GPU_AVAILABLE is None:
            _constants.cl, _constants.GPU_AVAILABLE = _check_gpu_available()
        try:
            import numpy as np
            if not _constants.GPU_AVAILABLE:
                return
            _cl = _constants.cl
            # Get available platforms and devices
            platforms = _cl.get_platforms()
            if not platforms:
                print("No OpenCL platforms found")
                return

            # Try to find a GPU device first, fallback to any device
            device = None
            for platform in platforms:
                try:
                    devices = platform.get_devices(_cl.device_type.GPU)
                    if devices:
                        device = devices[0]
                        print(f"Using GPU: {device.name}")
                        break
                except Exception:
                    continue

            # If no GPU found, try any device
            if device is None:
                for platform in platforms:
                    try:
                        devices = platform.get_devices()
                        if devices:
                            device = devices[0]
                            print(f"Using device: {device.name}")
                            break
                    except Exception:
                        continue

            if device is None:
                print("No suitable OpenCL device found")
                return

            # Create context and command queue
            self.context = _cl.Context([device])
            self.queue = _cl.CommandQueue(self.context)
            self.device = device

            # Get device limits
            self.max_buffer_size = device.max_mem_alloc_size
            print(f"GPU max buffer size: {self.max_buffer_size // (1024*1024)} MB")

            # Compile OpenCL kernel
            kernel_source = self._get_lut_kernel_source()
            self.program = _cl.Program(self.context, kernel_source).build()

            self.gpu_enabled = True
            print("GPU LUT processing initialized successfully")

            # Cache kernels for reuse to avoid repeated retrieval warning
            self._cache_kernels()

        except Exception as e:
            print(f"Failed to initialize GPU: {e}")
            print("GPU initialization details:")
            try:
                import traceback
                traceback.print_exc()

                # Try to get more detailed OpenCL information
                _cl = _constants.cl
                if _constants.GPU_AVAILABLE and _cl:
                    platforms = _cl.get_platforms()
                    print(f"Available OpenCL platforms: {len(platforms)}")
                    for i, platform in enumerate(platforms):
                        print(f"  Platform {i}: {platform.name}")
                        try:
                            devices = platform.get_devices()
                            print(f"    Devices: {len(devices)}")
                            for j, device in enumerate(devices):
                                print(f"      Device {j}: {device.name} ({device.vendor})")
                        except Exception as dev_e:
                            print(f"    Error getting devices: {dev_e}")
                else:
                    print("OpenCL not available")
            except Exception as debug_e:
                print(f"Error getting GPU debug info: {debug_e}")

            self.gpu_enabled = False

    def _cache_kernels(self):
        """Cache OpenCL kernels to avoid repeated retrieval"""
        _cl = _constants.cl
        if self.program and self.gpu_enabled:
            try:
                self._kernel_cache['apply_lut_3d'] = _cl.Kernel(self.program, 'apply_lut_3d')
                self._kernel_cache['draw_lines_gpu'] = _cl.Kernel(self.program, 'draw_lines_gpu')
                self._kernel_cache['assign_nearest_palette'] = _cl.Kernel(self.program, 'assign_nearest_palette')
                self._kernel_cache['assign_nearest_label'] = _cl.Kernel(self.program, 'assign_nearest_label')
                self._kernel_cache['labels_to_rgb'] = _cl.Kernel(self.program, 'labels_to_rgb')
                self._kernel_cache['smooth_labels_majority'] = _cl.Kernel(self.program, 'smooth_labels_majority')
                print("GPU kernels cached successfully")
            except Exception as e:
                print(f"Error caching kernels: {e}")

    def _get_lut_kernel_source(self):
        """OpenCL kernel source code for LUT processing"""
        return """
        __kernel void apply_lut_3d(
            __global uchar4* image,
                // IMPORTANT: use flat float array instead of float3 to avoid 16-byte stride padding issues
                __global const float* lut_data,
            const int width,
            const int height,
            const int lut_size,
            const float strength
        ) {
            int gid = get_global_id(0);

            if (gid >= width * height) return;

            // Get pixel - Qt RGB32 format stores BGRA bytes in memory
            uchar4 pixel = image[gid];

            // CONFIRMED: Channel mapping is correct!
            // GPU: pixel.x=Blue, pixel.y=Green, pixel.z=Red, pixel.w=Alpha
            float b = pixel.x / 255.0f;  // Blue
            float g = pixel.y / 255.0f;  // Green
            float r = pixel.z / 255.0f;  // Red

            // Clamp input values
            r = clamp(r, 0.0f, 1.0f);
            g = clamp(g, 0.0f, 1.0f);
            b = clamp(b, 0.0f, 1.0f);

                // High-quality trilinear interpolation
                // Position in LUT space (0 .. lut_size-1)
                float rf = r * (float)(lut_size - 1);
                float gf = g * (float)(lut_size - 1);
                float bf = b * (float)(lut_size - 1);

                int r0 = (int)floor(rf); int r1; float fr;
                if (r0 >= lut_size - 1) { r0 = lut_size - 2; r1 = lut_size - 1; fr = 1.0f; } else { r1 = r0 + 1; fr = rf - (float)r0; }
                int g0 = (int)floor(gf); int g1; float fg;
                if (g0 >= lut_size - 1) { g0 = lut_size - 2; g1 = lut_size - 1; fg = 1.0f; } else { g1 = g0 + 1; fg = gf - (float)g0; }
                int b0 = (int)floor(bf); int b1; float fb;
                if (b0 >= lut_size - 1) { b0 = lut_size - 2; b1 = lut_size - 1; fb = 1.0f; } else { b1 = b0 + 1; fb = bf - (float)b0; }

                // Helper lambda-like macro to fetch color (r,g,b indices)
                #define LUT_IDX(R,G,B) (((R) + (G) * lut_size + (B) * lut_size * lut_size) * 3)
                int i000 = LUT_IDX(r0,g0,b0);
                int i100 = LUT_IDX(r1,g0,b0);
                int i010 = LUT_IDX(r0,g1,b0);
                int i110 = LUT_IDX(r1,g1,b0);
                int i001 = LUT_IDX(r0,g0,b1);
                int i101 = LUT_IDX(r1,g0,b1);
                int i011 = LUT_IDX(r0,g1,b1);
                int i111 = LUT_IDX(r1,g1,b1);

                float3 c000 = (float3)(lut_data[i000+0], lut_data[i000+1], lut_data[i000+2]);
                float3 c100 = (float3)(lut_data[i100+0], lut_data[i100+1], lut_data[i100+2]);
                float3 c010 = (float3)(lut_data[i010+0], lut_data[i010+1], lut_data[i010+2]);
                float3 c110 = (float3)(lut_data[i110+0], lut_data[i110+1], lut_data[i110+2]);
                float3 c001 = (float3)(lut_data[i001+0], lut_data[i001+1], lut_data[i001+2]);
                float3 c101 = (float3)(lut_data[i101+0], lut_data[i101+1], lut_data[i101+2]);
                float3 c011 = (float3)(lut_data[i011+0], lut_data[i011+1], lut_data[i011+2]);
                float3 c111 = (float3)(lut_data[i111+0], lut_data[i111+1], lut_data[i111+2]);

                // Interpolate along R
                float3 c00 = c000 + (c100 - c000) * fr;
                float3 c10 = c010 + (c110 - c010) * fr;
                float3 c01 = c001 + (c101 - c001) * fr;
                float3 c11 = c011 + (c111 - c011) * fr;
                // Interpolate along G
                float3 c0 = c00 + (c10 - c00) * fg;
                float3 c1 = c01 + (c11 - c01) * fg;
                // Interpolate along B
                float3 c_final = c0 + (c1 - c0) * fb;

                float lut_r = c_final.x;
                float lut_g = c_final.y;
                float lut_b = c_final.z;
                #undef LUT_IDX

            // Apply strength blending
            float final_r, final_g, final_b;
            if (strength < 0.01f) {
                final_r = r;
                final_g = g;
                final_b = b;
            } else {
                final_r = r * (1.0f - strength) + lut_r * strength;
                final_g = g * (1.0f - strength) + lut_g * strength;
                final_b = b * (1.0f - strength) + lut_b * strength;
            }

            // Clamp final values
            final_r = clamp(final_r, 0.0f, 1.0f);
            final_g = clamp(final_g, 0.0f, 1.0f);
            final_b = clamp(final_b, 0.0f, 1.0f);

            // Write back in correct BGRA format
            image[gid].x = (uchar)(final_b * 255.0f);  // Blue
            image[gid].y = (uchar)(final_g * 255.0f);  // Green
            image[gid].z = (uchar)(final_r * 255.0f);  // Red
            image[gid].w = 255;  // Alpha
        }

        __kernel void draw_lines_gpu(
            __global uchar4* image,
            __global int* vertical_lines,
            __global int* horizontal_lines,
            __global int4* free_lines,  // start_x, start_y, end_x, end_y
            const int width,
            const int height,
            const int num_vertical,
            const int num_horizontal,
            const int num_free,
            const uchar4 line_color,
            const int line_thickness
        ) {
            int x = get_global_id(0);
            int y = get_global_id(1);

            if (x >= width || y >= height) return;

            int pixel_idx = y * width + x;
            bool draw_pixel = false;

            // Check vertical lines
            for (int i = 0; i < num_vertical; i++) {
                int line_x = vertical_lines[i];
                int distance = abs(x - line_x);
                if (distance <= line_thickness / 2) {
                    draw_pixel = true;
                    break;
                }
            }

            // Check horizontal lines
            if (!draw_pixel) {
                for (int i = 0; i < num_horizontal; i++) {
                    int line_y = horizontal_lines[i];
                    int distance = abs(y - line_y);
                    if (distance <= line_thickness / 2) {
                        draw_pixel = true;
                        break;
                    }
                }
            }

            // Check free lines (using distance to line segment)
            if (!draw_pixel) {
                for (int i = 0; i < num_free; i++) {
                    int4 line = free_lines[i];
                    int x1 = line.x, y1 = line.y, x2 = line.z, y2 = line.w;

                    // Calculate distance from point to line segment
                    int dx = x2 - x1;
                    int dy = y2 - y1;
                    int line_len_sq = dx * dx + dy * dy;

                    if (line_len_sq == 0) {
                        // Line is actually a point
                        int dist_sq = (x - x1) * (x - x1) + (y - y1) * (y - y1);
                        if (dist_sq <= (line_thickness / 2) * (line_thickness / 2)) {
                            draw_pixel = true;
                            break;
                        }
                    } else {
                        // Project point onto line segment
                        int t_num = (x - x1) * dx + (y - y1) * dy;
                        float t = (float)t_num / line_len_sq;
                        t = clamp(t, 0.0f, 1.0f);

                        int proj_x = x1 + (int)(t * dx);
                        int proj_y = y1 + (int)(t * dy);

                        int dist_sq = (x - proj_x) * (x - proj_x) + (y - proj_y) * (y - proj_y);
                        if (dist_sq <= (line_thickness / 2) * (line_thickness / 2)) {
                            draw_pixel = true;
                            break;
                        }
                    }
                }
            }

            // Draw the pixel if it's part of any line
            if (draw_pixel) {
                image[pixel_idx] = line_color;
            }
        }

        __kernel void assign_nearest_palette(
            __global const float* pixels,   // num_pixels * 3 (RGB)
            __global const float* palette,  // num_colors * 3 (RGB)
            __global uchar* out,            // num_pixels * 3 (RGB)
            const int num_pixels,
            const int num_colors
        ) {
            int gid = get_global_id(0);
            if (gid >= num_pixels) return;

            float pr = pixels[gid * 3 + 0];
            float pg = pixels[gid * 3 + 1];
            float pb = pixels[gid * 3 + 2];

            float best = 3.0e38f;
            int best_i = 0;
            for (int i = 0; i < num_colors; i++) {
                float dr = pr - palette[i * 3 + 0];
                float dg = pg - palette[i * 3 + 1];
                float db = pb - palette[i * 3 + 2];
                float d = dr * dr + dg * dg + db * db;
                if (d < best) { best = d; best_i = i; }
            }

            out[gid * 3 + 0] = (uchar)(clamp(palette[best_i * 3 + 0], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 1] = (uchar)(clamp(palette[best_i * 3 + 1], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 2] = (uchar)(clamp(palette[best_i * 3 + 2], 0.0f, 255.0f) + 0.5f);
        }

        __kernel void assign_nearest_label(
            __global const float* pixels,   // num_pixels * 3 (RGB)
            __global const float* palette,  // num_colors * 3 (RGB)
            __global int* labels,           // num_pixels
            const int num_pixels,
            const int num_colors
        ) {
            int gid = get_global_id(0);
            if (gid >= num_pixels) return;
            float pr = pixels[gid * 3 + 0];
            float pg = pixels[gid * 3 + 1];
            float pb = pixels[gid * 3 + 2];
            float best = 3.0e38f;
            int best_i = 0;
            for (int i = 0; i < num_colors; i++) {
                float dr = pr - palette[i * 3 + 0];
                float dg = pg - palette[i * 3 + 1];
                float db = pb - palette[i * 3 + 2];
                float d = dr * dr + dg * dg + db * db;
                if (d < best) { best = d; best_i = i; }
            }
            labels[gid] = best_i;
        }

        __kernel void labels_to_rgb(
            __global const int* labels,
            __global const float* palette,
            __global uchar* out,
            const int num_pixels,
            const int num_colors
        ) {
            int gid = get_global_id(0);
            if (gid >= num_pixels) return;
            int bi = labels[gid];
            out[gid * 3 + 0] = (uchar)(clamp(palette[bi * 3 + 0], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 1] = (uchar)(clamp(palette[bi * 3 + 1], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 2] = (uchar)(clamp(palette[bi * 3 + 2], 0.0f, 255.0f) + 0.5f);
        }

        __kernel void smooth_labels_majority(
            __global const int* labels,
            __global const float* palette,
            __global uchar* out,
            const int width,
            const int height,
            const int num_colors,
            const int radius
        ) {
            int x = get_global_id(0);
            int y = get_global_id(1);
            if (x >= width || y >= height) return;

            // Majority (mode) filter over a square window: pick the palette
            // label that occurs most within radius, rounding jagged field
            // boundaries into smooth curves. num_colors is capped at 32 by the
            // caller, so a private count array of 64 is always safe.
            int counts[64];
            for (int i = 0; i < num_colors; i++) counts[i] = 0;

            int x0 = max(0, x - radius);
            int x1 = min(width - 1, x + radius);
            int y0 = max(0, y - radius);
            int y1 = min(height - 1, y + radius);
            for (int ny = y0; ny <= y1; ny++) {
                int row = ny * width;
                for (int nx = x0; nx <= x1; nx++) {
                    counts[labels[row + nx]]++;
                }
            }
            int best = 0, bc = -1;
            for (int i = 0; i < num_colors; i++) {
                if (counts[i] > bc) { bc = counts[i]; best = i; }
            }
            int gid = y * width + x;
            out[gid * 3 + 0] = (uchar)(clamp(palette[best * 3 + 0], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 1] = (uchar)(clamp(palette[best * 3 + 1], 0.0f, 255.0f) + 0.5f);
            out[gid * 3 + 2] = (uchar)(clamp(palette[best * 3 + 2], 0.0f, 255.0f) + 0.5f);
        }
        """

    def apply_lut_gpu_quiet(self, image, lut_data, lut_size, strength_factor=1.0):
        """Apply LUT using GPU — silent version for high-frequency calls (GIF frames)."""
        if not self.gpu_enabled:
            return None
        import numpy as np
        try:
            width = image.width()
            height = image.height()
            image_array = self._qimage_to_numpy(image)

            # Cache the prepared LUT array to avoid re-converting every frame
            lut_cache_id = (id(lut_data), lut_size)
            if not hasattr(self, '_cached_lut_np') or self._cached_lut_id != lut_cache_id:
                self._cached_lut_np = self._prepare_lut_data_quiet(lut_data, lut_size)
                self._cached_lut_id = lut_cache_id

            return self._apply_lut_full(image_array, self._cached_lut_np, width, height, lut_size, strength_factor)
        except Exception:
            return None

    def _prepare_lut_data_quiet(self, lut_data, lut_size):
        """Convert LUT data to GPU format — no prints."""
        import numpy as np
        lut_array = np.array(lut_data, dtype=np.float32)
        return np.ascontiguousarray(lut_array, dtype=np.float32)

    def apply_lut_gpu(self, image, lut_data, lut_size, strength_factor=1.0):
        """Apply LUT using GPU acceleration"""
        if not self.gpu_enabled:
            return None

        import numpy as np
        _cl = _constants.cl
        try:
            width = image.width()
            height = image.height()
            total_pixels = width * height

            # Convert image to numpy array
            image_array = self._qimage_to_numpy(image)

            # Convert LUT data to numpy array
            lut_array = self._prepare_lut_data(lut_data, lut_size)

            # Optimized for 12GB GPU - conservative thresholds to avoid OUT_OF_RESOURCES
            image_size_bytes = image_array.nbytes
            lut_size_bytes = lut_array.nbytes
            total_size = image_size_bytes + lut_size_bytes

            # For 12GB GPU, process directly without chunking - simpler and more reliable
            print(f"Using direct GPU processing for {width}x{height} image ({total_pixels} pixels, {total_size//1024//1024}MB)")
            return self._apply_lut_full(image_array, lut_array, width, height, lut_size, strength_factor)

        except Exception as e:
            print(f"GPU LUT processing failed: {e}")
            return None

    def _qimage_to_numpy(self, qimage):
        """Convert QImage to numpy array for GPU processing"""
        import numpy as np
        # Use RGB32 format which is consistent and well-defined
        if qimage.format() != qimage.Format.Format_RGB32:
            qimage = qimage.convertToFormat(qimage.Format.Format_RGB32)

        width = qimage.width()
        height = qimage.height()

        # Get raw image data
        ptr = qimage.constBits()
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape((height, width, 4))

        # Create a copy to ensure it's writable
        return np.copy(arr)

    def _prepare_lut_data(self, lut_data, lut_size):
        """Convert LUT data to GPU format"""
        import numpy as np
        # Convert list of tuples to numpy array
        lut_array = np.array(lut_data, dtype=np.float32)

        # DEBUG: Print first few LUT values to check data format
        print(f"LUT DEBUG: First 3 LUT entries:")
        for i in range(min(3, len(lut_data))):
            print(f"  LUT[{i}]: {lut_data[i]} -> numpy: {lut_array[i]}")

        # Ensure it's the right shape for OpenCL float3
        expected_size = lut_size * lut_size * lut_size
        if lut_array.shape[0] != expected_size:
            raise ValueError(f"LUT data size mismatch: expected {expected_size}, got {lut_array.shape[0]}")

        # Ensure the array is contiguous and properly shaped for float3
        if lut_array.shape[1] != 3:
            raise ValueError(f"LUT data should have 3 components (RGB), got {lut_array.shape[1]}")

        # Make sure the array is C-contiguous for proper GPU transfer
        lut_array = np.ascontiguousarray(lut_array, dtype=np.float32)

        print(f"LUT DEBUG: Array shape: {lut_array.shape}, dtype: {lut_array.dtype}, contiguous: {lut_array.flags['C_CONTIGUOUS']}")
        return lut_array

    def _apply_lut_full(self, image_array, lut_array, width, height, lut_size, strength_factor):
        """Apply LUT using full image processing"""
        import numpy as np
        _cl = _constants.cl
        try:
            # Flatten image array for processing
            image_flat = image_array.reshape(-1, 4).astype(np.uint8)

            # Create OpenCL buffers
            image_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_WRITE | _cl.mem_flags.COPY_HOST_PTR, hostbuf=image_flat)
            lut_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY | _cl.mem_flags.COPY_HOST_PTR, hostbuf=lut_array)

            # Set kernel arguments using cached kernel
            kernel = self._kernel_cache.get('apply_lut_3d')
            if not kernel:
                kernel = _cl.Kernel(self.program, 'apply_lut_3d')
                self._kernel_cache['apply_lut_3d'] = kernel

            kernel.set_args(image_buffer, lut_buffer, np.int32(width), np.int32(height), np.int32(lut_size), np.float32(strength_factor))

            # Execute kernel
            global_size = (width * height,)
            _cl.enqueue_nd_range_kernel(self.queue, kernel, global_size, None)

            # Read result back
            result_flat = np.empty_like(image_flat)
            _cl.enqueue_copy(self.queue, result_flat, image_buffer)
            self.queue.finish()

            # Reshape back to original form
            result_array = result_flat.reshape(image_array.shape)

            # Convert back to QImage
            return self._numpy_to_qimage(result_array, width, height)

        except Exception as e:
            print(f"Full GPU processing error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _apply_lut_chunked(self, image_array, lut_array, width, height, lut_size, strength_factor):
        """Apply LUT using chunked processing for large images"""
        import numpy as np
        _cl = _constants.cl
        try:
            # Use reasonable chunk size for 12GB GPU - larger chunks for better performance
            rows_per_chunk = 200

            print(f"Processing {width}x{height} image in chunks of {rows_per_chunk} rows ({rows_per_chunk * width} pixels per chunk)")

            # Create LUT buffer once
            lut_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY | _cl.mem_flags.COPY_HOST_PTR, hostbuf=lut_array)

            result_array = np.copy(image_array)

            # Process in chunks
            for start_row in range(0, height, rows_per_chunk):
                end_row = min(start_row + rows_per_chunk, height)
                chunk_height = end_row - start_row

                # Extract chunk and flatten for GPU processing
                chunk_data = image_array[start_row:end_row, :, :].reshape(-1, 4).astype(np.uint8)

                # Create chunk buffer
                chunk_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_WRITE | _cl.mem_flags.COPY_HOST_PTR, hostbuf=chunk_data)

                # Set kernel arguments for chunked processing using cached kernel
                kernel = self._kernel_cache.get('apply_lut_chunked')
                if not kernel:
                    kernel = _cl.Kernel(self.program, 'apply_lut_chunked')
                    self._kernel_cache['apply_lut_chunked'] = kernel

                kernel.set_args(chunk_buffer, lut_buffer, np.int32(width), np.int32(height),
                               np.int32(lut_size), np.float32(strength_factor),
                               np.int32(start_row), np.int32(chunk_height))

                # Execute kernel with 1D work-group (more compatible)
                total_pixels = width * chunk_height
                global_size = (total_pixels,)
                _cl.enqueue_nd_range_kernel(self.queue, kernel, global_size, None)

                # Read chunk result back
                chunk_result_flat = np.empty_like(chunk_data)
                _cl.enqueue_copy(self.queue, chunk_result_flat, chunk_buffer)
                self.queue.finish()

                # Reshape and copy back to result
                chunk_result = chunk_result_flat.reshape((chunk_height, width, 4))
                result_array[start_row:end_row, :, :] = chunk_result

            return self._numpy_to_qimage(result_array, width, height)

        except Exception as e:
            print(f"Chunked GPU processing error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _numpy_to_qimage(self, array, width, height):
        """Convert numpy array back to QImage"""
        import numpy as np
        # Ensure data is contiguous
        if not array.flags['C_CONTIGUOUS']:
            array = np.ascontiguousarray(array)

        # Create QImage from numpy array using RGB32 format
        qimage = QImage(array.data, width, height, width * 4, QImage.Format.Format_RGB32)

        # Return a copy to ensure the data persists
        return qimage.copy()

    def draw_lines_gpu(self, image, vertical_lines, horizontal_lines, free_lines, line_color, line_thickness):
        """Draw lines on image using GPU acceleration"""
        if not self.gpu_enabled:
            return None

        import numpy as np
        _cl = _constants.cl
        try:
            width = image.width()
            height = image.height()

            # Ensure consistent RGB32 (BGRA) format to match kernel expectation
            if image.format() != image.Format.Format_RGB32:
                image = image.convertToFormat(image.Format.Format_RGB32)
            image_array = self._qimage_to_numpy(image)

            # Prepare line data
            vertical_array = np.array(vertical_lines, dtype=np.int32) if vertical_lines else np.array([], dtype=np.int32)
            horizontal_array = np.array(horizontal_lines, dtype=np.int32) if horizontal_lines else np.array([], dtype=np.int32)

            # Convert free lines to flat array [x1, y1, x2, y2, x1, y1, x2, y2, ...]
            free_array = np.array([], dtype=np.int32)
            if free_lines:
                free_flat = []
                for line in free_lines:
                    start_x, start_y = line['start']
                    end_x, end_y = line['end']
                    free_flat.extend([int(start_x), int(start_y), int(end_x), int(end_y)])
                free_array = np.array(free_flat, dtype=np.int32).reshape(-1, 4)

            # Pack color as BGRA for in-memory layout (Qt RGB32)
            color_bgra = np.array([line_color.blue(), line_color.green(), line_color.red(), line_color.alpha()], dtype=np.uint8)

            # Create OpenCL buffers
            image_flat = image_array.reshape(-1, 4).astype(np.uint8)
            image_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_WRITE | _cl.mem_flags.COPY_HOST_PTR, hostbuf=image_flat)

            # Create buffers for line data (handle empty arrays)
            if len(vertical_array) > 0:
                vertical_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY | _cl.mem_flags.COPY_HOST_PTR, hostbuf=vertical_array)
            else:
                vertical_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY, 4)

            if len(horizontal_array) > 0:
                horizontal_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY | _cl.mem_flags.COPY_HOST_PTR, hostbuf=horizontal_array)
            else:
                horizontal_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY, 4)

            if len(free_array) > 0:
                free_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY | _cl.mem_flags.COPY_HOST_PTR, hostbuf=free_array)
            else:
                free_buffer = _cl.Buffer(self.context, _cl.mem_flags.READ_ONLY, 16)  # 4 ints

            # Set kernel arguments using cached kernel to avoid repeated retrieval warning
            kernel = self._kernel_cache.get('draw_lines_gpu')
            if not kernel:
                kernel = _cl.Kernel(self.program, 'draw_lines_gpu')
                self._kernel_cache['draw_lines_gpu'] = kernel
            kernel.set_arg(0, image_buffer)
            kernel.set_arg(1, vertical_buffer)
            kernel.set_arg(2, horizontal_buffer)
            kernel.set_arg(3, free_buffer)
            kernel.set_arg(4, np.int32(width))
            kernel.set_arg(5, np.int32(height))
            kernel.set_arg(6, np.int32(len(vertical_lines) if vertical_lines else 0))
            kernel.set_arg(7, np.int32(len(horizontal_lines) if horizontal_lines else 0))
            kernel.set_arg(8, np.int32(len(free_lines) if free_lines else 0))
            kernel.set_arg(9, color_bgra)
            kernel.set_arg(10, np.int32(line_thickness))

            # Execute kernel
            global_size = (width, height)
            _cl.enqueue_nd_range_kernel(self.queue, kernel, global_size, None)

            # Read result back
            result_array = np.empty_like(image_flat)
            _cl.enqueue_copy(self.queue, result_array, image_buffer)
            self.queue.finish()

            # Convert back to QImage (RGB32/BGRA)
            result_array = result_array.reshape(height, width, 4)
            result_image = QImage(result_array.data, width, height, width * 4, QImage.Format.Format_RGB32)
            return QPixmap.fromImage(result_image)

        except Exception as e:
            print(f"GPU line drawing failed: {e}")
            return None

    def is_available(self):
        """Check if GPU processing is available, triggering lazy init on first call"""
        if not self._initialized:
            self._initialized = True
            self._initialize_gpu()
        return self.gpu_enabled

    def quantize_to_palette_gpu(self, pixels_rgb, palette):
        """Map each pixel to its nearest palette colour on the GPU.

        ``pixels_rgb`` is an (P, 3) array of RGB values (float or uint8);
        ``palette`` is a (K, 3) array of RGB palette colours. Returns a
        (P, 3) uint8 array holding each pixel's nearest palette colour, or
        ``None`` if the GPU is unavailable or the call fails (the caller then
        falls back to CPU). This is the per-pixel nearest-colour search used by
        the Color Groups effect — massively parallel and ideal for the GPU.
        """
        if not self.is_available():
            return None
        import numpy as np
        _cl = _constants.cl
        try:
            pixels = np.ascontiguousarray(pixels_rgb, dtype=np.float32).reshape(-1, 3)
            pal = np.ascontiguousarray(palette, dtype=np.float32).reshape(-1, 3)
            num_pixels = pixels.shape[0]
            num_colors = pal.shape[0]
            if num_pixels == 0 or num_colors == 0:
                return None
            out = np.empty((num_pixels, 3), dtype=np.uint8)

            mf = _cl.mem_flags
            pix_buf = _cl.Buffer(self.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=pixels)
            pal_buf = _cl.Buffer(self.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=pal)
            out_buf = _cl.Buffer(self.context, mf.WRITE_ONLY, out.nbytes)

            kernel = self._kernel_cache.get('assign_nearest_palette')
            if not kernel:
                kernel = _cl.Kernel(self.program, 'assign_nearest_palette')
                self._kernel_cache['assign_nearest_palette'] = kernel

            kernel.set_args(pix_buf, pal_buf, out_buf, np.int32(num_pixels), np.int32(num_colors))
            _cl.enqueue_nd_range_kernel(self.queue, kernel, (num_pixels,), None)
            _cl.enqueue_copy(self.queue, out, out_buf)
            self.queue.finish()
            return out
        except Exception as e:
            print(f"GPU palette quantization failed: {e}")
            return None

    def color_groups_gpu(self, pixels_rgb, palette, width, height, smooth_radius=0):
        """Quantize to a palette and optionally smooth colour fields — all GPU.

        Runs the whole Color Groups per-pixel workload on the GPU:
          1. ``assign_nearest_label`` — nearest palette colour per pixel (labels)
          2a. if ``smooth_radius`` > 0: ``smooth_labels_majority`` — a majority
              (mode) filter over a square window that rounds jagged field
              boundaries into smooth curves (this is the previously slow CPU
              per-colour box-blur step, now massively parallel on the GPU)
          2b. otherwise: ``labels_to_rgb`` — straight label→colour mapping

        ``pixels_rgb`` is an (H*W, 3) RGB array; ``palette`` is (K, 3) with
        K <= 32. Returns an (H*W, 3) uint8 array, or ``None`` if the GPU is
        unavailable/failed (caller falls back to CPU).
        """
        if not self.is_available():
            return None
        import numpy as np
        _cl = _constants.cl
        try:
            pixels = np.ascontiguousarray(pixels_rgb, dtype=np.float32).reshape(-1, 3)
            pal = np.ascontiguousarray(palette, dtype=np.float32).reshape(-1, 3)
            num_pixels = pixels.shape[0]
            num_colors = pal.shape[0]
            # Private count array in the smoothing kernel is sized 64.
            if num_pixels == 0 or num_colors == 0 or num_colors > 64:
                return None
            if num_pixels != width * height:
                return None

            mf = _cl.mem_flags
            pix_buf = _cl.Buffer(self.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=pixels)
            pal_buf = _cl.Buffer(self.context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=pal)
            lbl_buf = _cl.Buffer(self.context, mf.READ_WRITE, num_pixels * 4)
            out = np.empty((num_pixels, 3), dtype=np.uint8)
            out_buf = _cl.Buffer(self.context, mf.WRITE_ONLY, out.nbytes)

            def _get(name):
                k = self._kernel_cache.get(name)
                if not k:
                    k = _cl.Kernel(self.program, name)
                    self._kernel_cache[name] = k
                return k

            # 1. Assign nearest palette label per pixel.
            k_assign = _get('assign_nearest_label')
            k_assign.set_args(pix_buf, pal_buf, lbl_buf, np.int32(num_pixels), np.int32(num_colors))
            _cl.enqueue_nd_range_kernel(self.queue, k_assign, (num_pixels,), None)

            if smooth_radius and smooth_radius > 0:
                k_smooth = _get('smooth_labels_majority')
                k_smooth.set_args(lbl_buf, pal_buf, out_buf, np.int32(width), np.int32(height),
                                  np.int32(num_colors), np.int32(int(smooth_radius)))
                _cl.enqueue_nd_range_kernel(self.queue, k_smooth, (width, height), None)
            else:
                k_map = _get('labels_to_rgb')
                k_map.set_args(lbl_buf, pal_buf, out_buf, np.int32(num_pixels), np.int32(num_colors))
                _cl.enqueue_nd_range_kernel(self.queue, k_map, (num_pixels,), None)

            _cl.enqueue_copy(self.queue, out, out_buf)
            self.queue.finish()
            return out
        except Exception as e:
            print(f"GPU color groups failed: {e}")
            return None

    def get_device_info(self):
        """Get information about the GPU device"""
        if not self.gpu_enabled or not self.device:
            return "GPU not available"

        try:
            device_name = self.device.name
            device_vendor = self.device.vendor
            max_memory = self.device.max_mem_alloc_size // (1024 * 1024)
            compute_units = self.device.max_compute_units

            return f"{device_vendor} {device_name} ({max_memory}MB, {compute_units} CUs)"
        except Exception:
            return "GPU device info unavailable"

    def cleanup(self):
        """Clean up GPU resources"""
        if self.context:
            self.context = None
        if self.queue:
            self.queue = None
        self.gpu_enabled = False
