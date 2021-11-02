import itertools
from numba import jit
import numpy as np
import struct


def read_pfb(file, mode='full'):
    pfb = ParflowBinary(file)
    data = pfb.read_all_subgrids(mode=mode)
    pfb.close()
    return data


def read_stack_of_pfbs(file_seq):
    pfb_init = ParflowBinary(file_seq[0])
    base_header = pfb_init.header
    base_sg_offsets = pfb_init.subgrid_offsets
    base_sg_locations = pfb_init.subgrid_locations
    base_sg_indices = pfb_init.subgrid_start_indices
    base_sg_shapes = pfb_init.subgrid_shapes
    pfb_init.close()
    stack_size= (len(file_seq), base_header['nx'], base_header['ny'], base_header['nz'])
    pfb_stack = np.empty(stack_size, dtype=np.float64)
    for i, f in enumerate(file_seq):
        pfb = ParflowBinary(f, precompute_subgrid_info=False, header=base_header)
        pfb.subgrid_offsets = base_sg_offsets
        pfb.subgrid_locations = base_sg_locations
        pfb.subgrid_start_indices = base_sg_indices
        pfb.subgrid_shapes = base_sg_shapes
        pfb_stack[i, :, : ,:] = pfb.read_all_subgrids(mode='full')
        pfb.close()
    return pfb_stack


class ParflowBinary:

    def __init__(self, file, precompute_subgrid_info=True, p=None, q=None, r=None, header=None):
        self.filename = file
        self.f = open(self.filename, 'rb')
        if not header:
            self.header = self.read_header()
        else:
            self.header = header
            p = self.header.get('p', p)
            q = self.header.get('q', q)
            r = self.header.get('r', r)

        # If p, q, and r aren't given we can precompute them
        if not np.all([p, q, r]):
            eps = 1 - 1e-6
            first_sg_head = self.read_subgrid_header()
            self.header['p'] = int((self.header['nx'] / first_sg_head['nx']) + eps)
            self.header['q'] = int((self.header['ny'] / first_sg_head['ny']) + eps)
            self.header['r'] = int((self.header['nz'] / first_sg_head['nz']) + eps)

        if precompute_subgrid_info:
            self.compute_subgrid_info()

    def close(self):
        self.f.close()

    def compute_subgrid_info(self):
        sg_offs, sg_locs, sg_starts, sg_shapes = precalculate_subgrid_info(
                self.header['nx'],
                self.header['ny'],
                self.header['nz'],
                self.header['p'],
                self.header['q'],
                self.header['r'],
                self.header['n_subgrids']
        )
        self.subgrid_offsets = np.array(sg_offs)
        self.subgrid_locations = np.array(sg_locs)
        self.subgrid_start_indices = np.array(sg_starts)
        self.subgrid_shapes = np.array(sg_shapes)
        self.chunks = self._compute_chunks()
        self.coords = self._compute_coords()

    def _compute_chunks(self):
        p, q, r = self.header['p'], self.header['q'], self.header['r'],
        x_chunks = tuple(self.subgrid_shapes[:,0][0:p].flatten())
        y_chunks = tuple(self.subgrid_shapes[:,1][0:p*q:p].flatten())
        z_chunks = tuple(self.subgrid_shapes[:,2][0:p*q*r:p*q].flatten())
        return {'x': x_chunks, 'y': y_chunks, 'z': z_chunks}

    def _compute_coords(self):
        coords = {'x': [], 'y': [], 'z': []}
        for c in ['x', 'y', 'z']:
            chunk_start = 0
            for chunk in self.chunks[c]:
                coords[c].append(np.arange(chunk_start, chunk_start + chunk))
                chunk_start += chunk
        return coords

    def read_header(self):
        self.f.seek(0)
        header = {}
        header['x'] = struct.unpack('>d', self.f.read(8))[0]
        header['y'] = struct.unpack('>d', self.f.read(8))[0]
        header['z'] = struct.unpack('>d', self.f.read(8))[0]
        header['nx'] = struct.unpack('>i', self.f.read(4))[0]
        header['ny'] = struct.unpack('>i', self.f.read(4))[0]
        header['nz'] = struct.unpack('>i', self.f.read(4))[0]
        header['dx'] = struct.unpack('>d', self.f.read(8))[0]
        header['dy'] = struct.unpack('>d', self.f.read(8))[0]
        header['dz'] = struct.unpack('>d', self.f.read(8))[0]
        header['n_subgrids'] = struct.unpack('>i', self.f.read(4))[0]
        return header

    def read_subgrid_header(self, skip_bytes=64):
        self.f.seek(skip_bytes)
        sg_header = {}
        sg_header['ix'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['iy'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['iz'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['nx'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['ny'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['nz'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['rx'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['ry'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['rz'] = struct.unpack('>i', self.f.read(4))[0]
        sg_header['sg_size'] = np.prod([sg_header[n] for n in ['nx', 'ny', 'nz']])
        return sg_header

    def read_subarray(self, start_x, start_y, start_z=0, nx=1, ny=1, nz=None):
        """
        mirroring parflowio loadclipofdata
        determine which subgrid start is in
        determine which subgrid end is in
        determine padding from the subgrids

        As an example of what needs to happen here is
        the following image:
        +-------+-------+
        |       |       |
        |      x|xx     |
        +-------+-------+
        |      x|xx     |
        |      x|xx     |
        +-------+-------+
        Where each of the borders of the big grid are the
        four subgrids (2,2) that we are trying to index data from.
        The data to be selected falls in each of these subgrids, as
        denoted by the 'x' marks.
        """
        if not nz:
            nz = self.header['nz']
        end_x = start_x + nx
        end_y = start_y + ny
        end_z = start_z + nz
        p, q, r = self.header['p'], self.header['q'], self.header['r']

        # Determine which subgrids we need to read
        for p_start, xc in enumerate(self.coords['x']):
            if start_x in xc: break
        for p_end, xc in enumerate(self.coords['x']):
            if end_x in xc: break
        for q_start, yc in enumerate(self.coords['y']):
            if start_y in yc: break
        for q_end, yc in enumerate(self.coords['y']):
            if end_y in yc: break
        for r_start, zc in enumerate(self.coords['z']):
            if start_z in zc: break
        for r_end, zc in enumerate(self.coords['z']):
            if end_z in zc: break
        p_subgrids = np.arange(p_start, p_end+1)
        q_subgrids = np.arange(q_start, q_end+1)
        r_subgrids = np.arange(r_start, r_end+1)

        # Determine the coordinates of these subgrids
        x_coords = np.unique(np.hstack(np.array(self.coords['x'], dtype=object)[p_subgrids]))
        y_coords = np.unique(np.hstack(np.array(self.coords['y'], dtype=object)[q_subgrids]))
        z_coords = np.unique(np.hstack(np.array(self.coords['z'], dtype=object)[r_subgrids]))

        # Min values will be used to align in the bounding data
        x_min = np.min(x_coords)
        y_min = np.min(y_coords)
        z_min = np.min(z_coords)
        # Make an array which can fit all of the subgrids
        x_size_full = len(x_coords)
        y_size_full = len(y_coords)
        z_size_full = len(z_coords)

        bounding_data = np.empty((x_size_full, y_size_full, z_size_full), dtype=np.float64)
        subgrid_data = []
        ix, iy, iz = 0, 0, 0
        subgrid_iter = itertools.product(p_subgrids, q_subgrids, r_subgrids)
        for (xsg, ysg, zsg) in subgrid_iter:
            # Subgrid index
            i = xsg + (p * ysg) + (p * q * zsg)
            # Start location
            x0, y0, z0 = self.subgrid_start_indices[i]
            dx, dy, dz = self.subgrid_shapes[i]
            # Subtract off the minimum so we can insert into bounding_data
            x0 -= x_min
            y0 -= y_min
            z0 -= z_min
            # End location
            x1, y1, z1 = x0 + dx, y0 + dy, z0+ dz
            bounding_data[x0:x1, y0:y1, z0:z1] = self.iloc_subgrid(i)

        # Now clip out the exact part from the bounding box
        # TODO: Fix this jank
        try:
            x1 = np.argwhere(start_x == x_coords).flatten()[0]
        except:
            x1 = 0
        try:
            x2 = np.argwhere(end_x == x_coords).flatten()[0]
        except:
            x2 = -1
        clip_x = slice(x1, x2)

        try:
            y1 = np.argwhere(start_y == y_coords).flatten()[0]
        except:
            y1 = 0
        try:
            y2 = np.argwhere(end_y == y_coords).flatten()[0]
        except:
            y2 = -1
        clip_y = slice(y1, y2)

        try:
            z1 = np.argwhere(start_z == z_coords).flatten()[0]
        except:
            z1 = 0
        try:
            z2 = np.argwhere(end_z == z_coords).flatten()[0]
        except:
            z2 = 0
        clip_z = slice(z1, z2)
        return bounding_data[clip_x, clip_y, clip_z]


    def loc_subgrid(self, pp, qq, rr):
        p, q, r = self.header['p'], self.header['q'], self.header['r']
        subgrid_idx = pp + (p * qq) + (q * rr)
        return self.iloc_subgrid(subgrid_idx)

    def iloc_subgrid(self, idx):
        offset = self.subgrid_offsets[idx]
        shape = self.subgrid_shapes[idx]
        return self._backend_iloc_subgrid(offset, shape)

    def _backend_iloc_subgrid(self, offset, shape):
        mm = np.memmap(
            self.f,
            dtype=np.float64,
            mode='r',
            offset=offset,
            shape=tuple(shape),
            order='F'
        ).byteswap()
        data = np.array(mm)
        return data

    def read_all_subgrids(self, mode='full'):
        if mode not in ['flat', 'tiled', 'full']:
            raise Exception('mode must be one of flat, tiled, or full')
        if mode in ['flat', 'tiled']:
            all_data = []
            for i in range(self.header['n_subgrids']):
                all_data.append(self.iloc_subgrid(i))
            if mode == 'tiled':
                tiled_shape = tuple(self.header[dim] for dim in ['p', 'q', 'r'])
                all_data = np.array(all_data, dtype=object).reshape(tiled_shape)
        elif mode == 'full':
            full_shape = tuple(self.header[dim] for dim in ['nx', 'ny', 'nz'])
            chunks = self.chunks['x'], self.chunks['y'], self.chunks['z']
            all_data = np.empty(full_shape, dtype=np.float64)
            for i in range(self.header['n_subgrids']):
                nx, ny, nz = self.subgrid_shapes[i]
                ix, iy, iz = self.subgrid_start_indices[i]
                all_data[ix:ix+nx, iy:iy+ny, iz:iz+nz] = self.iloc_subgrid(i)
        return all_data

@jit(nopython=True)
def get_maingrid_and_remainder(nx, ny, nz, p, q, r):
    nnx = int(np.ceil(nx / p))
    nny = int(np.ceil(ny / q))
    nnz = int(np.ceil(nz / r))
    lx = (nx % p)
    ly = (ny % q)
    lz = (nz % r)
    return nnx, nny, nnz, lx, ly, lz

@jit(nopython=True)
def get_subgrid_loc(sel_subgrid, p, q, r):
    rr = int(np.floor(sel_subgrid / (p * q)))
    qq = int(np.floor((sel_subgrid - (rr*p*q)) / p))
    pp = int(sel_subgrid - rr * (p * q) - (qq * p))
    subgrid_loc = (pp, qq, rr)
    return subgrid_loc

@jit(nopython=True)
def subgrid_lower_left(
    nnx, nny, nnz,
    pp, qq, rr,
    lx, ly, lz
):
    ix = max(0, pp * (nnx-1) + min(pp, lx))
    iy = max(0, qq * (nny-1) + min(qq, ly))
    iz = max(0, rr * (nnz-1) + min(rr, lz))
    return ix, iy, iz

@jit(nopython=True)
def subgrid_size(
    nnx, nny, nnz,
    pp, qq, rr,
    lx, ly, lz
):
    snx = nnx-1 if pp >= max(lx, 1) else nnx
    sny = nny-1 if qq >= max(ly, 1) else nny
    snz = nnz-1 if rr >= max(lz, 1) else nnz
    return snx, sny, snz

@jit(nopython=True)
def precalculate_subgrid_info(nx, ny, nz, p, q, r, n_subgrids):
    subgrid_shapes = []
    subgrid_offsets = []
    subgrid_locs = []
    subgrid_begin_idxs = []
    # Initial size and offset for first subgrid
    snx, sny, snz = 0, 0, 0
    off = 64
    for sg_num in range(n_subgrids):
        # Move past the current header and previous subgrid
        off += 36 +  (8 * (snx * sny * snz))
        subgrid_offsets.append(off)

        nnx, nny, nnz, lx, ly, lz= get_maingrid_and_remainder(nx, ny, nz, p, q, r)
        pp, qq, rr = get_subgrid_loc(sg_num, p, q, r)
        subgrid_locs.append((pp, qq, rr))

        ix, iy, iz = subgrid_lower_left(
            nnx, nny, nnz,
            pp, qq, rr,
            lx, ly, lz
        )
        subgrid_begin_idxs.append((ix, iy, iz))

        snx, sny, snz = subgrid_size(
            nnx, nny, nnz,
            pp, qq, rr,
            lx, ly, lz
        )
        subgrid_shapes.append((snx, sny, snz))
    return subgrid_offsets, subgrid_locs, subgrid_begin_idxs, subgrid_shapes
