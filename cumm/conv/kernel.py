# Copyright 2021 Yan Yan
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, Union

import numpy as np
import pccm
from pccm.targets.cuda_ptx import RegDType

from cumm import cudasim, dtypes
from cumm import tensorview as tv
from cumm.common import (GemmBasic, GemmBasicKernel, TensorView,
                         TensorViewKernel)
from cumm.conv import input_iters
from cumm.conv.algospec import get_algo_spec
from cumm.conv.bases import (LAYOUT_TYPES, ConvEnum, ConvIterAlgo,
                             ConvIterParams, ConvLayout, ConvOpType)
from cumm.conv.params import ConvProblem
from cumm.core_cc.csrc.arrayref import ArrayPtr
from cumm.gemm import (codeops, constants, gemmmath, layout, mask_iters,
                       out_iters, output_op, thread_map, volta_iters,
                       volta_out_iters)
from cumm.gemm.algospec import GemmAlgo, TensorOpParams, bases
from cumm.gemm.blockmma import BlockMmaStorage, Mma
from cumm.gemm.core import MetaArray, array_type, metaseq, seq
from cumm.gemm.outputs import Output, OutputSmemStorage
from cumm.gemm.utils import GemmUtils


def div_up(a, b):
    return (a + b - 1) // b


class ConvParams(pccm.ParameterizedClass):
    def __init__(self,
                 problem: ConvProblem,
                 tile_shape: MetaArray[int],
                 dtype_a: dtypes.DType,
                 dtype_b: dtypes.DType,
                 dtype_c: dtypes.DType,
                 dtype_comp: dtypes.DType,
                 layout_a: LAYOUT_TYPES,
                 layout_b: LAYOUT_TYPES,
                 layout_c: LAYOUT_TYPES,
                 itera_params: ConvIterParams,
                 iterb_params: ConvIterParams,
                 out_params: out_iters.OutIteratorParams,
                 have_workspace: bool = False,
                 mask_sparse: bool = False):
        super().__init__()
        self.add_dependency(TensorView, GemmBasic, ConvEnum)
        self.ndim = problem.ndim
        self.op_type = problem.op_type
        self.problem = problem
        self.itera_params = itera_params
        self.iterb_params = iterb_params
        self.out_params = out_params
        self.layout_a = layout_a
        self.layout_b = layout_b
        self.layout_c = layout_c
        self.mask_sparse = mask_sparse

        self.add_param_class("cp", problem, "ConvProblem")
        self.add_param_class("itera_p", itera_params, "IterAParams")
        self.add_param_class("iterb_p", iterb_params, "IterBParams")

        self.add_param_class("out_params_ns", out_params, "OutIterParams")
        self.add_param_class("la", layout_a, "LayoutA")
        self.add_param_class("lb", layout_b, "LayoutB")
        self.add_param_class("lc", layout_c, "LayoutC")
        self.add_param_class("gemmutils", GemmUtils(tile_shape), "GemmUtils")

        self.tile_shape = tile_shape
        self.dtype_a = dtype_a
        self.dtype_b = dtype_b
        self.dtype_c = dtype_c
        self.dtype_comp = dtype_comp

        self.add_member("problem", "ConvProblem")
        # if mask_sparse and self.op_type == ConvOpType.kBackwardWeight:
        #     self.add_member("m, n, k, gemm_k_size_per_split", "int")
        # else:
        self.add_member("m, n, k, gemm_k_iterations", "int")
        self.add_member("ptr_A", f"const {dtype_a}*")
        self.add_member("ptr_B", f"const {dtype_b}*")
        self.add_member("ptr_C", f"{dtype_c}*")
        self.add_member("ptr_D", f"{dtype_c}*")
        if mask_sparse:
            self.add_member("mask_ptr", f"const uint32_t*")
            if problem.op_type == ConvOpType.kForward:
                self.add_member("mask_out_ptr", f"uint32_t*")
            self.add_member("RS", f"int")

        self.add_member("alpha, beta", f"{dtype_comp}")
        self.add_member("grid_dims", f"dim3")
        self.have_workspace = have_workspace
        if have_workspace:
            self.add_member("workspace", "void*")

        self.add_member("itera_params_", f"IterAParams")
        self.add_member("iterb_params_", f"IterBParams")
        self.add_member("out_params_", f"OutIterParams")

        # cudasim members
        self.problem_size_: Optional[ConvProblem] = None
        self.m = 0
        self.n = 0
        self.k = 0
        self.gemm_k_iterations = 0
        self.ptr_A: Optional[ArrayPtr] = None
        self.ptr_B: Optional[ArrayPtr] = None
        self.ptr_C: Optional[ArrayPtr] = None
        self.ptr_D: Optional[ArrayPtr] = None
        self.alpha = 0
        self.beta = 0
        self.grid_dims = cudasim.Dim3(0, 0, 0)
        self.itera_params_: Optional[ConvIterParams] = None
        self.iterb_params_: Optional[ConvIterParams] = None
        self.out_params_: Optional[out_iters.OutIteratorParams] = None

    def python_ctor(self,
                    problem: ConvProblem,
                    A: ArrayPtr,
                    B: ArrayPtr,
                    C: ArrayPtr,
                    D: ArrayPtr,
                    alpha: float,
                    beta: float,
                    split_k_slice: int = 1):
        new_obj = ConvParams(self.problem, self.tile_shape, self.dtype_a,
                             self.dtype_b, self.dtype_c, self.dtype_comp,
                             self.layout_a, self.layout_b, self.layout_c,
                             self.itera_params, self.iterb_params,
                             self.out_params, self.have_workspace)
        mnk = problem.implicit_gemm_mnk_python(self.op_type)
        new_obj.problem_size_ = problem
        m = mnk[0]
        n = mnk[1]
        k = mnk[2]
        new_obj.grid_dims.x = codeops.div_up(m, new_obj.tile_shape[0])
        new_obj.grid_dims.y = codeops.div_up(n, new_obj.tile_shape[1])
        new_obj.grid_dims.z = split_k_slice
        gemm_k_iterations = problem.implicit_gemm_k_iterations_python(
            self.op_type, new_obj.tile_shape[2])
        new_obj.gemm_k_iterations = gemm_k_iterations
        new_obj.ptr_A = A
        new_obj.ptr_B = B
        new_obj.ptr_C = C
        new_obj.ptr_D = D
        new_obj.m = m
        new_obj.n = n
        new_obj.k = k
        new_obj.alpha = alpha
        new_obj.beta = beta
        if self.op_type == ConvOpType.kForward:
            a_shape = problem.get_input_shape_python()
            b_shape = problem.get_weight_shape_python()
        elif self.op_type == ConvOpType.kBackwardInput:
            a_shape = problem.get_output_shape_python()
            b_shape = problem.get_weight_shape_python()
        elif self.op_type == ConvOpType.kBackwardWeight:
            a_shape = problem.get_input_shape_python()
            b_shape = problem.get_output_shape_python()
        else:
            raise NotImplementedError
        new_obj.itera_params_ = self.itera_params.python_ctor(
            problem, self.layout_a.from_shape_python(a_shape))
        new_obj.iterb_params_ = self.iterb_params.python_ctor(
            problem, self.layout_b.from_shape_python(b_shape))

        new_obj.out_params_ = self.out_params.python_ctor(n)
        return new_obj

    @pccm.cuda.constructor
    def ctor(self):
        print(self.tile_shape)
        code = pccm.FunctionCode()
        code.arg("problem", "ConvProblem")
        code.arg("A", f"const {self.dtype_a}*")
        code.arg("B", f"const {self.dtype_b}*")
        code.arg("C", f"{self.dtype_c}*")
        code.arg("D", f"{self.dtype_c}*")
        if self.mask_sparse:
            code.arg("mask_ptr", f"const uint32_t*")
            code.arg("mask_argsort_ptr", f"const int*")
            code.arg("indice_ptr", f"const int*")
            if self.op_type == ConvOpType.kForward:
                code.arg("mask_out_ptr", f"uint32_t*")

        code.arg("alpha", f"{self.dtype_comp}", f"{self.dtype_comp}(1)")
        code.arg("beta", f"{self.dtype_comp}", f"{self.dtype_comp}(0)")
        code.arg("split_k_slice", "int", "1")
        if self.have_workspace:
            code.arg("workspace", "void*", "nullptr")
        code.ctor_init("problem", "problem")
        if self.op_type == ConvOpType.kForward:
            if self.mask_sparse:
                code.ctor_init("itera_params_",
                               "problem, indice_ptr, mask_argsort_ptr")
                code.ctor_init("mask_out_ptr", "mask_out_ptr")

            else:
                code.ctor_init(
                    "itera_params_",
                    "problem, LayoutA::from_shape(problem.get_input_shape())")
            code.ctor_init(
                "iterb_params_",
                "problem, LayoutB::from_shape(problem.get_weight_shape())")
        elif self.op_type == ConvOpType.kBackwardInput:
            if self.mask_sparse:
                code.ctor_init("itera_params_",
                               "problem, indice_ptr, mask_argsort_ptr")
            else:
                code.ctor_init(
                    "itera_params_",
                    "problem, LayoutA::from_shape(problem.get_output_shape())")
            code.ctor_init(
                "iterb_params_",
                "problem, LayoutB::from_shape(problem.get_weight_shape())")
        elif self.op_type == ConvOpType.kBackwardWeight:
            if self.mask_sparse:
                code.ctor_init("itera_params_",
                               "problem, indice_ptr, mask_argsort_ptr")
                code.ctor_init("iterb_params_",
                               "problem, indice_ptr, mask_argsort_ptr")
            else:
                code.ctor_init(
                    "itera_params_",
                    "problem, LayoutA::from_shape(problem.get_output_shape())")
                code.ctor_init(
                    "iterb_params_",
                    "problem, LayoutB::from_shape(problem.get_input_shape())")
        else:
            raise NotImplementedError

        code.ctor_init("ptr_A", "A")
        code.ctor_init("ptr_B", "B")
        code.ctor_init("ptr_C", "C")
        code.ctor_init("ptr_D", "D")
        if self.mask_sparse:
            code.ctor_init("mask_ptr", "mask_ptr")

        code.ctor_init("alpha", "alpha")
        code.ctor_init("beta", "beta")
        if self.have_workspace:
            code.ctor_init("workspace", "workspace")
        optype_to_cpp = {
            ConvOpType.kForward: "ConvEnum::OpType::kForward",
            ConvOpType.kBackwardWeight: "ConvEnum::OpType::kBackwardWeight",
            ConvOpType.kBackwardInput: "ConvEnum::OpType::kBackwardInput",
        }
        code.raw(f"""
        auto mnk = problem.implicit_gemm_mnk({optype_to_cpp[self.op_type]});
        m = mnk[0];
        n = mnk[1];
        k = mnk[2];
        """)
        # if not (self.mask_sparse and self.op_type == ConvOpType.kBackwardWeight):
        code.raw(f"""
        gemm_k_iterations = problem.implicit_gemm_k_iterations({optype_to_cpp[self.op_type]}, {self.tile_shape[2]});
        """)
        if self.mask_sparse:
            # assert self.op_type == ConvOpType.kForward
            C_or_K = "C" if self.op_type == ConvOpType.kForward else "K"
            code.raw(f"RS = tv::arrayops::prod(problem.ksize);")
            if self.op_type == ConvOpType.kBackwardWeight:
                # for backward weight, we need to ensure the whole block is inside only one filter offset.
                # output is A, input is B (row major), so the block contiguous is tile_shape[1]
                # code.raw(f"gemm_k_size_per_split = GemmUtils::get_gemm_k_size_per_split(k, split_k_slice);")
                code.raw(
                    f"TV_ASSERT_RT_ERR(problem.C % {self.tile_shape[1]} == 0, \"error\");"
                )
            else:
                code.raw(
                    f"TV_ASSERT_INVALID_ARG(gemm_k_iterations % RS == 0, \"error\");"
                )
                code.raw(
                    f"TV_ASSERT_RT_ERR(problem.{C_or_K} % (split_k_slice * {self.tile_shape[2]}) == 0, \"error\");"
                )
        code.raw("grid_dims = get_logical_tile_count(m, n, k, split_k_slice);")
        code.raw(f"""
        tv::ssprint("gemm_k_size", m, n, k, split_k_slice, gemm_k_iterations, grid_dims.x, grid_dims.y, grid_dims.z);
        """)
        if self.mask_sparse and not self.op_type == ConvOpType.kBackwardWeight:
            code.raw(f"gemm_k_iterations /= RS;")
            code.raw("out_params_ = OutIterParams(n, mask_argsort_ptr);")
        else:
            # code.raw(f"""
            # TV_THROW_RT_ERR("WTF");
            # """)
            code.raw("out_params_ = OutIterParams(n);")
        return code

    @pccm.cuda.static_function
    def get_logical_tile_count(self):
        code = pccm.FunctionCode()
        code.arg("m,n,k,split_k_slice", "int")
        code.ret("dim3")
        code.raw(f"""
        dim3 grid_dims;
        grid_dims.x = tv::div_up(m, {self.tile_shape[0]});
        grid_dims.y = tv::div_up(n, {self.tile_shape[1]});
        grid_dims.z = split_k_slice;
        return grid_dims;
        """)
        return code


class ConvKernel(pccm.ParameterizedClass):
    def __init__(self,
                 ndim: int,
                 op_type: ConvOpType,
                 iter_algo: ConvIterAlgo,
                 tile_shape: MetaArray[int],
                 warp_tile_shape: MetaArray[int],
                 num_stage: int,
                 dtype_a: dtypes.DType,
                 dtype_b: dtypes.DType,
                 dtype_c: dtypes.DType,
                 dtype_acc: dtypes.DType,
                 dtype_comp: dtypes.DType,
                 layout_desp_input: ConvLayout,
                 layout_desp_weight: ConvLayout,
                 layout_desp_output: ConvLayout,
                 tensorop: Optional[TensorOpParams] = None,
                 algo: GemmAlgo = GemmAlgo.Simt,
                 splitk_serial: bool = False,
                 splitk_parallel: bool = False,
                 need_source: bool = True,
                 mask_sparse: bool = False,
                 increment_k_first: bool = False,
                 mask_width: int = -1):
        """
        splitK and sliceK:
        https://github.com/NVIDIA/cutlass/issues/211#issuecomment-801992218
        split K: multiple block in k axis
        slice K: multiple warp in k axis

        Convolution kernel Don't support simulation for two reasons:
        1. conv kernel only change the input iterator (and interleaved layout for NCxHWx),
           the code is easy to understand and debug without simulation and visualization.
        2. conv kernel simulation is very slow.
        """
        super().__init__()
        self.add_dependency(TensorView, TensorViewKernel, layout.RowMajor,
                            layout.ColumnMajor, GemmBasicKernel)
        self.need_source = need_source
        problem = ConvProblem(ndim, op_type, layout_desp_input,
                              layout_desp_weight, layout_desp_output,
                              mask_sparse)
        self.problem = problem
        trans_a, trans_b, trans_c = problem.get_gemm_trans_abc()
        self.tile_shape = tile_shape
        self.warp_tile_shape = warp_tile_shape
        self.num_stage = num_stage
        self.iter_algo = iter_algo
        self.tensorop = tensorop
        self.splitk_serial = splitk_serial
        self.splitk_parallel = splitk_parallel
        have_workspace = splitk_serial or splitk_parallel
        transpose_gemm = trans_c
        self.mask_sparse = mask_sparse
        self.increment_k_first = increment_k_first
        self.mask_width = mask_width
        if transpose_gemm:
            self.dtype_a = dtype_b
            self.dtype_b = dtype_a
            trans_a = not trans_a
            trans_b = not trans_b
            tmp = trans_a
            trans_a = trans_b
            trans_b = tmp
            trans_c = not trans_c
        else:
            self.dtype_a = dtype_a
            self.dtype_b = dtype_b
        dtype_a = self.dtype_a
        dtype_b = self.dtype_b

        self.dtype_c = dtype_c
        self.dtype_acc = dtype_acc
        self.dtype_comp = dtype_comp
        self.trans_a = trans_a
        self.trans_b = trans_b
        self.trans_c = trans_c
        self.algo = algo
        algo_spec = get_algo_spec(self.algo)(problem, tile_shape,
                                             warp_tile_shape, num_stage,
                                             dtype_a, dtype_b, dtype_c,
                                             dtype_acc, dtype_comp, iter_algo,
                                             tensorop, algo, mask_sparse,
                                             increment_k_first)
        self.algo_spec = algo_spec
        self.input_spec = algo_spec.input_spec
        self.mma_spec = algo_spec.mma_spec
        self.output_spec = algo_spec.output_spec

        self.warp_count_shape = tile_shape // warp_tile_shape
        self.warp_count = self.warp_count_shape.prod()
        self.num_threads = self.warp_count * constants.WARP_SIZE
        self.partk = self.warp_count_shape[2]
        self.add_param_class("inpitera", self.input_spec.input_iter_a,
                             "InputIteratorA")
        self.add_param_class("inpiterb", self.input_spec.input_iter_b,
                             "InputIteratorB")
        self.add_param_class("layouta", self.input_spec.layout_a, "LayoutA")
        self.add_param_class("layoutb", self.input_spec.layout_b, "LayoutB")
        self.layout_c = problem.get_c_layout_class()
        self.add_param_class("layoutc", self.layout_c, "LayoutC")

        padding_mn = self.mma_spec.padding_mn

        self.acc_frag_iter = self.output_spec.acc_frag_iter
        self.out_warp_tile_iter = self.output_spec.out_warp_tile_iter
        out_smem_padding = self.output_spec.smem_padding
        self.fragment_c_t = array_type(
            dtype_acc, self.output_spec.get_accumulator_count())
        self.gemm_smem_storage = BlockMmaStorage(tile_shape,
                                                 seq(0, padding_mn[0]),
                                                 seq(0, padding_mn[1]),
                                                 num_stage, dtype_a, dtype_b)
        self.out_smem_storage = OutputSmemStorage(
            seq(
                tile_shape[0] // self.output_spec.num_out_iters *
                self.warp_count_shape[2], tile_shape[1]), out_smem_padding,
            dtype_acc, self.output_spec.frag_per_iter)
        # if partk > 1, we need more smem tile to save each k result.
        # self.frag_per_iter = self.output_spec.frag_per_iter
        # self.out_num_tile = self.output_spec.frag_per_iter if self.output_spec.frag_per_iter > 1 else self.partk
        # self.out_tile_size = self.out_smem_storage.smem_size // dtype_acc.itemsize() // self.out_num_tile
        print(self.out_smem_storage.smem_size,
              self.gemm_smem_storage.smem_size)
        self.smem_size = max(self.out_smem_storage.smem_size,
                             self.gemm_smem_storage.smem_size)
        self.add_param_class("gemm_smem_storage", self.gemm_smem_storage,
                             "BlockMmaStorage")
        self.add_param_class("out_smem_storage", self.out_smem_storage,
                             "OutputSmemStorage")
        inp_iter_a_param = self.input_spec.input_iter_a.get_params()
        inp_iter_b_param = self.input_spec.input_iter_b.get_params()

        self.gemm_params = ConvParams(problem, tile_shape, dtype_a, dtype_b,
                                      dtype_c, dtype_comp,
                                      self.input_spec.layout_a,
                                      self.input_spec.layout_b, self.layout_c,
                                      inp_iter_a_param, inp_iter_b_param,
                                      self.output_spec.out_iter.get_params(),
                                      have_workspace, mask_sparse)
        self.add_param_class("conv_params", self.gemm_params, "ConvParams")
        # first_input_clear: for gemm, we don't need to clear frag in every input load
        # but gemm need it. gemm clear frag in iter.load, so we don't need
        # initial clear in mma.
        self.mma_container = Mma(
            dtype_acc,
            self.partk,
            num_stage,
            self.mma_spec,
            self.gemm_smem_storage,
            first_input_clear=False,
            clear_mask=False,
            mask_sparse=self.mask_sparse,
            increment_k_first=increment_k_first,
            mask_width=mask_width,
            is_sparse_wgrad=self.problem.op_type == ConvOpType.kBackwardWeight)
        self.output = Output(dtype_acc, self.warp_count_shape, self.partk,
                             self.output_spec, self.out_smem_storage)
        self.add_param_class("out_iter", self.output_spec.out_iter, "OutIter")
        self.add_param_class("out_iter_const", self.output_spec.const_out_iter,
                             "ConstOutIter")
        self.add_param_class("out_op", self.output_spec.output_op, "OutputOp")

        self.add_param_class("mma", self.mma_container, "Mma")
        self.add_param_class("output", self.output, "Output")

    def get_algo_name(self):
        res = f"{self.algo.value}_{self.dtype_a.shortcut()}{self.dtype_b.shortcut()}{self.dtype_c.shortcut()}"
        res += f"{self.dtype_acc.shortcut()}{self.dtype_comp.shortcut()}"
        las = "n" if self.trans_a else "t"
        lbs = "n" if self.trans_b else "t"
        lcs = "n" if self.trans_c else "t"
        res += f"{las}{lbs}{lcs}"
        res += f"_m{self.tile_shape[0]}n{self.tile_shape[1]}k{self.tile_shape[2]}"
        res += f"m{self.warp_tile_shape[0]}n{self.warp_tile_shape[1]}k{self.warp_tile_shape[2]}"
        if self.tensorop is not None:
            tes = self.tensorop.shape
            res += f"T{tes[0]}{tes[1]}{tes[2]}"
        res += f"_{self.num_stage}"
        res += f"_C{self.problem.ndim}_{self.problem.op_type.value}{self.iter_algo.value}"
        if self.mask_sparse:
            res += "_F" if not self.increment_k_first else "_K"
        return res

    @pccm.cuda.cuda_global_function  # (inline=True)
    def conv_kernel(self):
        code = pccm.cuda.PTXCode()
        # code.add_pre_attr(f"__launch_bounds__({self.num_threads}, 4)")
        code.arg("params", "ConvParams")
        code.raw(f"""
        constexpr bool kSplitKSerial = {pccm.boolean(self.splitk_serial)};
        extern __shared__ uint8_t SharedStorage[];
        auto gemm_shared_mem =
            reinterpret_cast<BlockMmaStorage *>(SharedStorage);
        auto out_shared_mem =
            reinterpret_cast<OutputSmemStorage *>(SharedStorage);

        int tile_offset_m = blockIdx.x;
        int tile_offset_n = blockIdx.y;
        int tile_offset_k = blockIdx.z;
        if (tile_offset_m >= params.grid_dims.x ||
            tile_offset_n >= params.grid_dims.y) {{
            return;
        }}
        """)
        k_offset = f"tile_offset_k * {self.tile_shape[2]}"
        m_offset = f"tile_offset_m * {self.tile_shape[0]}"
        n_offset = f"tile_offset_n * {self.tile_shape[1]}"
        if self.trans_a:
            a_offset = f"{k_offset}, {m_offset}"
        else:
            a_offset = f"{m_offset}, {k_offset}"
        if self.trans_b:
            b_offset = f"{n_offset}, {k_offset}"
        else:
            if self.mask_sparse and self.problem.op_type == ConvOpType.kBackwardWeight:
                code.raw(
                    f"int num_block_in_C = tv::div_up(params.problem.C, {self.tile_shape[1]});"
                )
                b_offset = f"{k_offset}, (tile_offset_n % num_block_in_C) * {self.tile_shape[1]}"
            else:
                b_offset = f"{k_offset}, {n_offset}"
        code.raw(f"""
        tv::array<int, 2> block_offset_A{{{a_offset}}};
        tv::array<int, 2> block_offset_B{{{b_offset}}};
        """)
        # if self.trans_a:
        #     code.raw(f"""
        #     tv::array<int, 2> block_offset_A{{tile_offset_k * {self.tile_shape[2]},
        #                                     tile_offset_m * {self.tile_shape[0]}}};
        #     """)
        # else:
        #     code.raw(f"""
        #     tv::array<int, 2> block_offset_A{{tile_offset_m * {self.tile_shape[0]},
        #                                     tile_offset_k * {self.tile_shape[2]}}};
        #     """)
        # if self.trans_b:
        #     code.raw(f"""
        #     tv::array<int, 2> block_offset_B{{tile_offset_n * {self.tile_shape[1]},
        #                                     tile_offset_k * {self.tile_shape[2]}}};
        #     """)
        # else:
        #     if self.mask_sparse and self.problem.op_type == ConvOpType.kBackwardWeight:
        #         # for pre comp convs, we can't add offset to input with implicit filter dims because
        #         # it's handled by indices.
        #         code.raw(f"""
        #         tv::array<int, 2> block_offset_B{{tile_offset_k * {self.tile_shape[2]}, 0}};
        #         """)
        #     else:
        #         code.raw(f"""
        #         tv::array<int, 2> block_offset_B{{tile_offset_k * {self.tile_shape[2]},
        #                                         tile_offset_n * {self.tile_shape[1]}}};
        #         """)
        code.raw(f"""
        int thread_idx = threadIdx.x;
        """)
        code.raw(f"""
        InputIteratorA input_iter_A(
            params.itera_params_, params.problem, params.ptr_A,
            thread_idx,
            block_offset_A);
        InputIteratorB input_iter_B(
            params.iterb_params_, params.problem, params.ptr_B,
            thread_idx,
            block_offset_B);
        """)
        code.raw(f"""
        int warp_idx = __shfl_sync(0xffffffff, threadIdx.x / 32, 0);
        int lane_idx = threadIdx.x % 32;
        int warp_mn =
            warp_idx % ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
        int warp_idx_k =
            warp_idx / ({self.warp_count_shape[0]} * {self.warp_count_shape[1]});
        int warp_m = warp_mn % {self.warp_count_shape[0]};
        int warp_n = warp_mn / {self.warp_count_shape[0]};
        """)
        if self.mask_sparse:
            if not self.problem.op_type == ConvOpType.kBackwardWeight:
                num_mask_per_thread = self.tile_shape[0] // constants.WARP_SIZE
                assert num_mask_per_thread > 0
                code.raw(f"""
                uint32_t kmask = 0;
                tv::array<uint32_t, {num_mask_per_thread}> masks;
                masks.clear();
                TV_PRAGMA_UNROLL
                for (int i = 0; i < {num_mask_per_thread}; ++i){{
                    if (tile_offset_m * {self.tile_shape[0]} + i * {constants.WARP_SIZE} + lane_idx < params.m){{
                        masks[i] = params.mask_ptr[tile_offset_m * {self.tile_shape[0]} + i * {constants.WARP_SIZE} + lane_idx];
                    }}
                }}
                TV_PRAGMA_UNROLL
                for (int i = 0; i < {num_mask_per_thread}; ++i){{
                    kmask |= masks[i];
                }}
                // perform a warp reduce to get block mask
                TV_PRAGMA_UNROLL
                for (int mask = {constants.WARP_SIZE // 2}; mask > 0; mask /= 2) {{
                    kmask |= __shfl_xor_sync(0xffffffff, kmask, mask, 32);
                }}
                """)
                if self.problem.op_type == ConvOpType.kForward:
                    # we need to save mask which will be used in backward weight.
                    code.raw(f"params.mask_out_ptr[tile_offset_m] = kmask;")
                code.raw(f"""
                if (kmask == 0){{
                    return;
                }}
                """)
                if self.problem.op_type == ConvOpType.kBackwardInput:
                    # reverse kmask
                    code.raw(
                        f"""kmask = __brev(kmask) >> ({32} - params.RS);""")
            else:
                # read mask of last residual  tile
                code.raw(f"""
                uint32_t kmask = params.mask_ptr[(tv::div_up(params.problem.N, {self.mask_width})) - 1];
                int filter_offset = tile_offset_n / (params.problem.C / {self.tile_shape[1]});
                """)
        code.raw(f"""
        Mma mma(gemm_shared_mem, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx);
        {self.fragment_c_t} accumulators;
        accumulators.clear();
        """)
        with code.if_("!kSplitKSerial || params.gemm_k_iterations > 0"):
            if self.mask_sparse:
                if not self.problem.op_type == ConvOpType.kBackwardWeight:
                    code.raw(f"""
                    mma(params.gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators, kmask, params.RS);
                    """)
                else:
                    code.raw(f"""
                    int num_reduced_mask = tv::div_up(params.problem.N, {self.mask_width});
                    mma(params.gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators, 
                        params.mask_ptr, num_reduced_mask, tile_offset_k, gridDim.z, filter_offset);
                    """)
            else:
                code.raw(f"""
                mma(params.gemm_k_iterations, accumulators, input_iter_A, input_iter_B, accumulators);
                """)
        code.raw(f"""
        // if (threadIdx.x == 3)
        // tv::print_fragment_meta_once<float>(accumulators, "accumulator");
        """)

        code.raw(f"""
        // // C = alpha * A@B + beta * D, D can be C
        OutputOp output_op(params.alpha, params.beta);
        """)
        if self.splitk_serial:
            code.raw(f"""
            int block_idx = tile_offset_m + tile_offset_n * params.grid_dims.x;
            tv::Semaphore semaphore(reinterpret_cast<int*>(params.workspace) + block_idx, thread_idx);
            if (params.grid_dims.z > 1){{
                semaphore.fetch();
                output_op.set_k_partition(tile_offset_k, params.grid_dims.z);
            }}
            """)
        code.raw(f"""
        tv::array<int, 2> block_offset_C{{tile_offset_m * {self.tile_shape[0]},
                                        tile_offset_n * {self.tile_shape[1]}}};

        OutIter out_iter_C(params.out_params_, params.ptr_C, {{params.m, params.n}},
                                {{block_offset_C[0], block_offset_C[1]}},
                                thread_idx);
        """)
        if self.splitk_serial:
            code.raw(f"""
            bool need_self_reduce = false;
            if (params.grid_dims.z > 1){{
                if (tile_offset_k){{
                    need_self_reduce = true;
                }}
                semaphore.wait(tile_offset_k);
                __threadfence();
            }}
            """)
        if self.need_source:
            code.raw(f"""
            ConstOutIter out_iter_D(params.out_params_, params.ptr_D, {{params.m, params.n}},
                                {{block_offset_C[0], block_offset_C[1]}},
                                thread_idx);
            """)
        code.raw(
            f"Output out(out_shared_mem, thread_idx, warp_idx_k, warp_m, warp_n, lane_idx);"
        )
        if self.splitk_serial:
            with code.if_("need_self_reduce"):
                code.raw(
                    f"out.run_self_reduce(output_op, accumulators, out_iter_C);"
                )
            with code.else_():
                if self.need_source:
                    code.raw(
                        f"out.run(output_op, accumulators, out_iter_C, out_iter_D);"
                    )
                else:
                    code.raw(f"out.run(output_op, accumulators, out_iter_C);")
        else:
            if self.need_source:
                code.raw(
                    f"out.run(output_op, accumulators, out_iter_C, out_iter_D);"
                )
            else:
                code.raw(f"out.run(output_op, accumulators, out_iter_C);")

        if self.splitk_serial:
            code.raw(f"""
            if (params.grid_dims.z > 1){{
                int lock = 0;
                if (params.grid_dims.z == tile_offset_k + 1) {{
                    // The final threadblock resets the semaphore for subsequent grids.
                    lock = 0;
                }}
                else {{
                    // Otherwise, the semaphore is incremented
                    lock = tile_offset_k + 1;
                }}
                
                semaphore.release(lock);
            }}
            """)
        return code

    # @lineprof.lineprof_wrapper
    async def conv_kernel_python(self, params: ConvParams):
        smem = cudasim.get_smem()
        gemm_storage = self.gemm_smem_storage
        smem_A = smem[:gemm_storage.smem_size_a].view(
            dtypes.get_npdtype(self.dtype_a))
        assert smem_A.nbytes == gemm_storage.smem_size_a
        smem_B = smem[gemm_storage.smem_size_a:gemm_storage.smem_size].view(
            dtypes.get_npdtype(self.dtype_b))
        assert smem_B.nbytes == gemm_storage.smem_size_b
        out_storage = self.out_smem_storage
        smem_out = smem[:out_storage.smem_size].view(
            dtypes.get_npdtype(self.dtype_acc))
        if cudasim.enable_debug():
            smem_A_ptr = ArrayPtr(self.dtype_a.tv_dtype,
                                  smem_A.nbytes // self.dtype_a.itemsize(),
                                  external_data=tv.from_numpy(smem_A))
            smem_B_ptr = ArrayPtr(self.dtype_b.tv_dtype,
                                  smem_B.nbytes // self.dtype_b.itemsize(),
                                  external_data=tv.from_numpy(smem_B))
        else:
            smem_A_ptr = ArrayPtr(self.dtype_a.tv_dtype,
                                  smem_A.nbytes // self.dtype_a.itemsize(),
                                  external_data=tv.from_numpy(smem_A),
                                  meta_data=tv.Tensor())
            smem_B_ptr = ArrayPtr(self.dtype_b.tv_dtype,
                                  smem_B.nbytes // self.dtype_b.itemsize(),
                                  external_data=tv.from_numpy(smem_B),
                                  meta_data=tv.Tensor())

        thread_idx = cudasim.threadIdx().x

        # share smem metadata in block
        smem_A_ptr = await cudasim.block_broadcast(smem_A_ptr)
        smem_B_ptr = await cudasim.block_broadcast(smem_B_ptr)

        smem_out_ptr = ArrayPtr(self.dtype_acc.tv_dtype,
                                smem_out.nbytes // self.dtype_acc.itemsize(),
                                external_data=tv.from_numpy(smem_out))

        tile_offset_m = cudasim.blockIdx().x
        tile_offset_n = cudasim.blockIdx().y
        tile_offset_k = cudasim.blockIdx().z
        if (tile_offset_m >= params.grid_dims.x
                or tile_offset_n >= params.grid_dims.y):
            return

        block_offset_A = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_k * self.tile_shape[2])
        if self.trans_a:
            block_offset_A = block_offset_A[::-1]
        block_offset_B = seq(tile_offset_k * self.tile_shape[2],
                             tile_offset_n * self.tile_shape[1])
        if self.trans_b:
            block_offset_B = block_offset_B[::-1]

        block_offset_C = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_n * self.tile_shape[1])
        block_offset_D = seq(tile_offset_m * self.tile_shape[0],
                             tile_offset_n * self.tile_shape[1])
        gemm_k_iterations = params.gemm_k_iterations
        input_iter_A = self.input_spec.input_iter_a.python_ctor(
            params.itera_params_, params.problem_size_, params.ptr_A,
            thread_idx, block_offset_A)
        input_iter_B = self.input_spec.input_iter_b.python_ctor(
            params.iterb_params_, params.problem_size_, params.ptr_B,
            thread_idx, block_offset_B)
        warp_idx = cudasim.get_warp_id()
        lane_idx = thread_idx % 32
        await cudasim.syncthreads()
        warp_idx_k = warp_idx // (self.warp_count_shape[0] *
                                  self.warp_count_shape[1])
        warp_mn = warp_idx % (self.warp_count_shape[0] *
                              self.warp_count_shape[1])
        warp_m = warp_mn % self.warp_count_shape[0]
        warp_n = warp_mn // self.warp_count_shape[0]
        mma = await self.mma_container.python_ctor(smem_A_ptr, smem_B_ptr,
                                                   thread_idx, warp_idx_k,
                                                   warp_m, warp_n, lane_idx)
        accumulators = ArrayPtr(self.dtype_acc.tv_dtype,
                                self.mma_spec.accumulator_size)
        accumulators.clear()
        res_inputs = await mma(gemm_k_iterations, accumulators, input_iter_A,
                               input_iter_B, accumulators)

        await cudasim.syncthreads()
        if cudasim.threadIdx().x == 0:
            acc = accumulators.data.numpy_view()
            cudasim.debug_print("accumulators", acc.mean(), acc.max(),
                                acc.min())

        output_op = self.output_spec.output_op.python_ctor(
            params.alpha, params.beta)

        out_iter_C = self.output_spec.out_iter.python_ctor(
            params.out_params_, params.ptr_C, seq(params.m, params.n),
            seq(block_offset_C[0], block_offset_C[1]), thread_idx)
        output = self.output.python_ctor(smem_out_ptr, thread_idx, warp_idx_k,
                                         warp_m, warp_n, lane_idx)
        res_output = await output(output_op, accumulators, out_iter_C)
        if not cudasim.enable_debug():
            return

        res = {
            **res_output,
            **res_inputs,
        }
        return res
