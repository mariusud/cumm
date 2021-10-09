import pccm
import abc
from cumm import dtypes
from cumm.constants import CUTLASS_MODE
from cumm.gemm.core import metaseq, seq, MetaArray, array_type
from cumm.gemm.thread_map import PitchLinear, PitchLinearWarpRaked
from typing import Optional, Union
from cumm.core_cc.csrc.arrayref import ArrayPtr


@pccm.skip_inherit
class DTypeBase(pccm.ParameterizedClass):
    def __init__(self,
                 dtype: dtypes.DType,
                 element_per_acc: int,
                 alignment: int = 0):
        super().__init__()
        self.dtype = dtype
        self.element_per_acc = element_per_acc
        self.pointer = f"{dtype} *"
        self.const_pointer = f"const {dtype} *"
        self.byte_pointer = f"char *"
        self.const_byte_pointer = f"const char *"
        self.long_index_t = dtypes.int64
        self.index_t = dtypes.int32
        if alignment == -1:
            alignment = element_per_acc * dtype.bitsize() // 8
        if CUTLASS_MODE:
            if alignment > 0:
                fmt = "cutlass::AlignedArray<{}, {}, {}>"
            else:
                fmt = "cutlass::Array<{}, {}>"
            self.access_t = fmt.format(dtype, element_per_acc, alignment)
            self.access_pointer = f"{self.access_t} *"
            self.const_access_pointer = f"const {self.access_t} *"
        else:
            fmt = "tv::array<{}, {}, {}>"
            # here we must use int4/2/1 instead of standard float16 array
            # to make sure expected ptx is generated.
            # if we use float16[8] as a 128bit access type,
            # ptx may contains st.shared.u16, u32, etc
            # if use int4/2/1, correct st.shared.v4.u32 is generated.
            if element_per_acc * dtype.bitsize() == 128:
                self.access_t = fmt.format("int4", 1, alignment)
            elif element_per_acc * dtype.bitsize() == 64:
                self.access_t = fmt.format("int2", 1, alignment)
            elif element_per_acc * dtype.bitsize() == 32:
                self.access_t = fmt.format("int", 1, alignment)
            else:
                self.access_t = fmt.format(dtype, element_per_acc, alignment)
            self.access_pointer = f"{self.access_t} *"
            self.const_access_pointer = f"const {self.access_t} *"


@pccm.skip_inherit
class GemmIterator(DTypeBase):
    """
    Iterator Base
    
    """
    def __init__(self,
                 dtype: dtypes.DType,
                 fragment_length: int,
                 element_per_acc: int,
                 alignment: int = 0):
        super().__init__(dtype, element_per_acc, alignment)
        self.element_count = fragment_length
        assert fragment_length > 0, "zero size frag length isn't allowed"
        self.fragment_t = array_type(dtype, self.element_count)


@pccm.skip_inherit
class GemmInputIterator(GemmIterator):
    """
    Iterator Base
    
    """
    def __init__(self,
                 dtype: dtypes.DType,
                 tmap: Union[PitchLinearWarpRaked, PitchLinear],
                 sub_tile_shape: MetaArray[int],
                 fragment_length: int,
                 element_per_acc: int,
                 alignment: int = 0):
        super().__init__(dtype, fragment_length, element_per_acc, alignment)
        self.tmap = tmap
        self.sub_tile_shape = sub_tile_shape

    def get_params(self) -> pccm.ParameterizedClass:
        raise NotImplementedError

    def python_ctor(self,
                    params,
                    ptr: ArrayPtr,
                    extent: MetaArray[int],
                    thread_id: int,
                    tb_offset: MetaArray[int],
                    is_left: bool = True) -> "GemmInputIterator":
        raise NotImplementedError

    def tile_increment_python(self, num_tile: int):
        raise NotImplementedError

    def clear_mask_python(self):
        raise NotImplementedError

    def increment_python(self):
        return self.tile_increment_python(1)

    def load_python(self, frag: ArrayPtr):
        raise NotImplementedError


@pccm.skip_inherit
class GemmSmemIterator(GemmIterator):
    def python_ctor(self, stride: int, ptr: ArrayPtr,
                    thread_id: int) -> "GemmSmemIterator":
        raise NotImplementedError

    def tile_increment_python(self, num_tile: int):
        raise NotImplementedError

    def increment_python(self):
        return self.tile_increment_python(1)

    async def store_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def get_smem_vis_shape(self) -> MetaArray[int]:
        raise NotImplementedError


@pccm.skip_inherit
class GemmWarpIterator(GemmIterator):
    async def python_ctor(self, ptr: ArrayPtr, warp_idx_k: int,
                          warp_idx_residual: int,
                          lane_idx: int) -> "GemmWarpIterator":
        raise NotImplementedError

    def tile_increment_python(self, num_tile: int):
        raise NotImplementedError

    def increment_python(self):
        return self.tile_increment_python(1)

    async def load_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def set_wmma_k_index_python(self, wmma_k: int):
        raise NotImplementedError


@pccm.skip_inherit
class GemmOutputIterator(GemmIterator):
    """
    Iterator Base
    
    """
    def get_params(self) -> pccm.ParameterizedClass:
        raise NotImplementedError

    def python_ctor(self, params, ptr: ArrayPtr, extent: MetaArray[int],
                    offset_2d: MetaArray[int],
                    thread_idx: int) -> "GemmOutputIterator":
        raise NotImplementedError

    def store_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def load_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def increment_python(self):
        raise NotImplementedError


@pccm.skip_inherit
class GemmOutWarpIterator(GemmIterator):
    def python_ctor(self, ptr: ArrayPtr, warp_offset_m: int,
                    warp_offset_n: int,
                    lane_idx: int) -> "GemmOutWarpIterator":
        raise NotImplementedError

    async def store_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def add_pointer_offset_python(self, pointer_offset: int):
        raise NotImplementedError


@pccm.skip_inherit
class GemmOutSmemLoader(GemmIterator):
    def python_ctor(self, ptr: ArrayPtr,
                    thread_idx: int) -> "GemmOutSmemLoader":
        raise NotImplementedError

    async def load_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def add_pointer_offset_python(self, pointer_offset: int):
        raise NotImplementedError


@pccm.skip_inherit
class GemmOutFragIterator(GemmIterator):
    def python_ctor(self, src_ptr: ArrayPtr) -> "GemmOutFragIterator":
        raise NotImplementedError

    def load_python(self, frag: ArrayPtr):
        raise NotImplementedError

    def increment_python(self):
        raise NotImplementedError


@pccm.skip_inherit
class WarpMma(pccm.ParameterizedClass):
    def python_ctor(self) -> "WarpMma":
        raise NotImplementedError

    async def __call__(self, D: ArrayPtr, A: ArrayPtr, B: ArrayPtr,
                       C: ArrayPtr):
        raise NotImplementedError


@pccm.skip_inherit
class GemmOutputOp(pccm.ParameterizedClass):
    def python_ctor(self, alpha: float, beta: float) -> "GemmOutputOp":
        raise NotImplementedError

    def call_op_source_python(self, accumulator: ArrayPtr, source: ArrayPtr):
        raise NotImplementedError

    def call_op_nosource_python(self, accumulator: ArrayPtr):
        raise NotImplementedError


@pccm.skip_inherit
class GemmApply(pccm.ParameterizedClass):
    def python_ctor(self) -> "GemmApply":
        raise NotImplementedError

    def apply_output_operator_python(self, output_fragment: ArrayPtr,
                                     output_op: GemmOutputOp,
                                     aligned_accum_fragment: ArrayPtr,
                                     source_fragment: ArrayPtr):
        raise NotImplementedError

    def apply_output_operator_no_source_python(
            self, output_fragment: ArrayPtr, output_op: GemmOutputOp,
            aligned_accum_fragment: ArrayPtr):
        raise NotImplementedError