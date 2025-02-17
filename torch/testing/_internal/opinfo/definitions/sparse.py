import torch
from torch.testing import make_tensor  # noqa: F401
from torch.testing._internal.opinfo.core import (  # noqa: F401
    ErrorInput,
    ReductionOpInfo,
    sample_inputs_reduction,
    SampleInput,
)


def _check_validate(op_info, sample):
    def _check_fail(sample):
        try:
            op_info(
                sample.sample_input.input,
                *sample.sample_input.args,
                **sample.sample_input.kwargs,
            )
        except sample.error_type:
            pass
        except Exception as msg:
            raise AssertionError(
                f"{op_info.name} on {sample.sample_input=} expected exception "
                f"{sample.error_type}: {sample.error_regex}, got {type(msg).__name__}: {msg}"
            )
        else:
            raise AssertionError(
                f"{op_info.name} on {sample.sample_input=} expected exception "
                f"{sample.error_type}: {sample.error_regex}, got none."
            )

    def _check_success(sample):
        try:
            op_info(sample.input, *sample.args, **sample.kwargs)
        except Exception as msg:
            raise AssertionError(
                f"{op_info.name} on {sample=} expected to succeed "
                f", got {type(msg).__name__}: {msg}"
            )

    if isinstance(sample, ErrorInput):
        _check_fail(sample)
    else:
        _check_success(sample)


def _sample_inputs_sparse(
    sample_inputs,
    maybe_failing_sample_inputs,
    validate_sample_input,
    op_info,
    *args,
    **kwargs,
):
    check_validate = False
    for sample in sample_inputs(op_info, *args, **kwargs):
        sample = validate_sample_input(op_info, sample, check_validate=check_validate)
        if isinstance(sample, SampleInput):
            yield sample
        # Error inputs are handled in error_inputs_sparse

    for sample in maybe_failing_sample_inputs(op_info, *args, **kwargs):
        sample = validate_sample_input(op_info, sample, check_validate=check_validate)
        if isinstance(sample, SampleInput):
            yield sample


def _error_inputs_sparse(
    maybe_failing_sample_inputs, validate_sample_input, op_info, *args, **kwargs
):
    check_validate = False
    for sample in maybe_failing_sample_inputs(op_info, *args, **kwargs):
        sample = validate_sample_input(op_info, sample, check_validate=check_validate)
        if isinstance(sample, ErrorInput):
            yield sample
        # Sample inputs are handled in sample_inputs_sparse


def sample_inputs_sparse_reduction(
    op_info, device, dtype, requires_grad, layout, blocksize=None, **kwargs
):
    """Sample inputs for reduction operations on sparse tensors."""
    layout_name = str(layout).split(".", 1)[-1].rsplit("_coo", 1)[0]
    op_supports_layout = getattr(op_info, "supports_" + layout_name)
    if not op_supports_layout:
        return

    for sample_input in sample_inputs_reduction(
        op_info, device, dtype, requires_grad, **kwargs
    ):
        if sample_input.input.ndim == 0:
            # scalar sparse tensors are not supported
            continue

        if layout in {
            torch.sparse_csr,
            torch.sparse_csc,
            torch.sparse_bsr,
            torch.sparse_bsc,
        }:
            if sample_input.input.ndim < 2:
                # conversion to sparse compressed tensors requires at
                # least 2 dimensional tensors
                continue
            if sample_input.input.ndim > 2 and (sample_input.input == 0).any():
                # Skip batched sparse compressed samples that contain
                # explicit zeros because to_sparse(layout=..) will
                # fail, see gh-98495.
                # TODO: remove this if-block after gh-98495 is fixed.
                continue

        if layout in {torch.sparse_bsr, torch.sparse_bsc} and blocksize is None:
            blocksize = (1, 1)

        yield SampleInput(
            sample_input.input.detach()
            .to_sparse(layout=layout, blocksize=blocksize)
            .requires_grad_(requires_grad),
            args=sample_input.args,
            kwargs=sample_input.kwargs,
        )

        if layout is torch.sparse_coo and (dtype.is_floating_point or dtype.is_complex):
            # uncoalesced samples
            inp = sample_input.input.detach().to_sparse(layout=layout)
            inp = torch.sparse_coo_tensor(
                inp.indices().repeat(1, 2),
                inp.values().repeat(2),
                inp.shape,
                dtype=inp.dtype,
                device=inp.device,
            )
            assert not inp.is_coalesced()
            yield SampleInput(
                inp.requires_grad_(requires_grad),
                args=sample_input.args,
                kwargs=sample_input.kwargs,
            )

        if sample_input.input.ndim > 2:
            # hybrid samples
            yield SampleInput(
                sample_input.input.detach()
                .to_sparse(
                    layout=layout,
                    blocksize=blocksize,
                    dense_dim=sample_input.input.ndim - 2,
                )
                .requires_grad_(requires_grad),
                args=sample_input.args,
                kwargs=sample_input.kwargs,
            )


def _validate_sample_input_sparse_reduction(op_info, sample, check_validate=False):
    """Return the specified sample when it is valid and supported by the
    operation. Otherwise, return the sample as ErrorInput instance.

    When check_validate is True, the result is validated against
    calling the op on the sample.
    """
    UNSPECIFIED = object()
    if op_info.name == "sum":
        sample = _validate_sample_input_sparse_reduction_sum(sample)

    if op_info.name in {"masked.sum"}:
        mask = sample.kwargs.get("mask", UNSPECIFIED)
        if (
            mask not in {None, UNSPECIFIED}
            and mask.ndim > 2
            and mask.layout is torch.strided
            and (mask == 0).any()
        ):
            # TODO: remove this if-block after gh-98495 is fixed.
            sample = ErrorInput(
                sample,
                error_regex="Expect the same number of specified elements per batch.",
            )
        elif not sample.kwargs.get("keepdim"):
            sample = ErrorInput(
                sample,
                error_type=(AssertionError, RuntimeError),
                error_regex="reduction operations on (CSR|CSC) tensors with keepdim=False is unsupported",
            )
        elif mask is UNSPECIFIED:
            sample = ErrorInput(
                sample,
                error_type=ValueError,
                error_regex="masked (.*) expects explicit mask for sparse_csr tensor input",
            )
        elif sample.input.ndim > 2:
            sample = ErrorInput(
                sample,
                error_regex="crow_indices is supposed to be a vector, but got 3 dimensional tensor.",
            )

    if op_info.name in {"masked.amax", "masked.amin", "masked.mean", "masked.prod"}:
        t_inp = sample.input
        batch_dim = t_inp.dim() - t_inp.dense_dim() - t_inp.sparse_dim()
        mask = sample.kwargs.get("mask")
        if (
            mask is not None
            and mask.ndim > 2
            and mask.layout is torch.strided
            and (mask == 0).any()
        ):
            # TODO: remove this if-block after gh-98495 is fixed.
            sample = ErrorInput(
                sample,
                error_regex="Expect the same number of specified elements per batch.",
            )
        elif mask is None:
            sample = ErrorInput(
                sample,
                error_type=ValueError,
                error_regex="masked (.*) expects explicit mask for sparse_csr tensor input",
            )
        elif (
            mask.layout is sample.input.layout
            and mask.ndim > 2
            and op_info.name == "masked.mean"
        ):
            sample = ErrorInput(
                sample,
                error_type=TypeError,
                error_regex=(
                    "where[(][)] received an invalid combination of arguments"
                    " - got [(]Tensor, Tensor, NoneType[)]"
                ),
            )
        elif not sample.kwargs.get("keepdim"):
            sample = ErrorInput(
                sample,
                error_type=(AssertionError, RuntimeError),
                error_regex="reduction operations on (CSR|CSC) tensors with keepdim=False is unsupported",
            )
        elif (
            sample.input.ndim > 2
            and (sample.kwargs.get("dim") not in {0, 1})
            and mask.ndim > 2
            and mask.layout is not torch.strided
        ):
            if sample.kwargs.get("dim") == (0, -1):
                sample = ErrorInput(
                    sample,
                    error_regex="tensor dimensionality must be sum of batch, base, and dense dimensionalities",
                )
            elif op_info.name == "masked.prod":
                sample = ErrorInput(
                    sample,
                    error_regex="input_dim == 2 INTERNAL ASSERT FAILED at",
                )
            else:
                sample = ErrorInput(
                    sample,
                    error_type=AssertionError,
                    error_regex="Sparse CSR tensors are 2D and only support reduction along dim 0 or 1.",
                )
        elif sample.input.ndim > 2:
            sample = ErrorInput(
                sample,
                error_regex="crow_indices is supposed to be a vector, but got 3 dimensional tensor.",
            )
        elif (
            mask.layout is t_inp.layout
            and mask._nnz() != t_inp._nnz()
            and t_inp.dense_dim() > 0
        ):
            sample = ErrorInput(
                sample,
                error_regex="Index tensor must have the same number of dimensions as src tensor",
            )

    if check_validate:
        _check_validate(op_info, sample)

    return sample


def _validate_sample_input_sparse_reduction_sum(sample, check_validate=False):
    # NOTE: When fixing a failing sample case, remove the
    #       corresponding if-block
    t_inp, t_args, t_kwargs = sample.input, sample.args, sample.kwargs
    dim = t_kwargs.get("dim")
    keepdim = t_kwargs.get("keepdim")
    layout = t_inp.layout
    if layout in {
        torch.sparse_csr,
        torch.sparse_csc,
        torch.sparse_bsr,
        torch.sparse_bsc,
    }:
        if (isinstance(dim, int) and (t_inp.dim() != 2 or keepdim)) or (
            isinstance(dim, (list, tuple))
            and (((t_inp.dim() != 2 and len(dim) != t_inp.dim()) or keepdim))
        ):
            if layout in {torch.sparse_bsr, torch.sparse_bsc}:
                return ErrorInput(
                    sample,
                    error_regex=(
                        "empty_sparse_compressed expected sparse compressed [(]non-block[)] tensor"
                        " layout but got Sparse(Bsr|Bsc)"
                    ),
                )
            else:
                return ErrorInput(
                    sample,
                    error_type=NotImplementedError,
                    error_regex="Could not run 'aten::sum.IntList_out' with arguments from the 'SparseCsr(CPU|CUDA)' backend",
                )
        elif t_kwargs and not keepdim:
            # reductions on sparse compressed tensors require
            # keepdim==True when reduction is over sparse dimensions
            return ErrorInput(
                sample,
                # FIXME: raise a better exception message
                error_regex="torch.empty: Only batched sparse compressed [(]non-block[)] tensors are supported",
            )
    return sample


def _maybe_failing_sample_inputs_sparse_reduction_sum(
    op_info, device, dtype, requires_grad, layout, **kwargs
):
    """Generator of samples that are known to fail or that were failing in past."""
    # NOTE: When fixing a failing case, remove the Exception comment
    #       but keep the `yield sample` statement.
    if layout in [
        torch.sparse_csr,
        torch.sparse_csc,
    ]:
        # NotImplementedError: Could not run 'aten::sum.IntList_out' with arguments from the 'SparseCsrCPU' backend.
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0, keepdim=True),
        )
        yield SampleInput(
            torch.tensor([[[0, 1]], [[2, 3]]], dtype=dtype)
            .to_sparse(layout=layout, dense_dim=1)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0),
        )
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,)),
        )
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,), keepdim=True),
        )
        yield SampleInput(
            torch.tensor([[[0, 1]], [[2, 3]]], dtype=dtype)
            .to_sparse(layout=layout, dense_dim=1)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,)),
        )

        # RuntimeError: torch.empty: Only batched sparse compressed (non-block) tensors are supported, but got size [2]
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0),
        )

    if layout in [
        torch.sparse_bsr,
        torch.sparse_bsc,
    ]:
        # RuntimeError: empty_sparse_compressed expected sparse compressed (non-block) tensor layout but got SparseBsr
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout, blocksize=(2, 2))
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0, keepdim=True),
        )
        yield SampleInput(
            torch.tensor([[[0, 1]], [[2, 3]]], dtype=dtype)
            .to_sparse(layout=layout, dense_dim=1, blocksize=(1, 1))
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0),
        )
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout, blocksize=(1, 1))
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,)),
        )
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout, blocksize=(1, 1))
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,), keepdim=True),
        )
        yield SampleInput(
            torch.tensor([[[0, 1]], [[2, 3]]], dtype=dtype)
            .to_sparse(layout=layout, blocksize=(1, 1), dense_dim=1)
            .requires_grad_(requires_grad),
            kwargs=dict(dim=(0,)),
        )

        # RuntimeError: torch.empty: Only batched sparse compressed (non-block) tensors are supported, but got size [2]
        yield SampleInput(
            torch.tensor([[0, 1], [2, 3]], dtype=dtype)
            .to_sparse(layout=layout, blocksize=(1, 1))
            .requires_grad_(requires_grad),
            kwargs=dict(dim=0),
        )


def sample_inputs_sparse_reduction_sum(
    op_info, device, dtype, requires_grad, layout, **kwargs
):
    """Sample inputs for sum on sparse tensors."""
    yield from _sample_inputs_sparse(
        sample_inputs_sparse_reduction,
        _maybe_failing_sample_inputs_sparse_reduction_sum,
        _validate_sample_input_sparse_reduction,
        op_info,
        device,
        dtype,
        requires_grad,
        layout,
        **kwargs,
    )


def error_inputs_sparse_reduction_sum(op_info, device, layout, **kwargs):
    """Error inputs for sum on sparse tensors."""
    dtype = torch.float64
    requires_grad = False
    yield from _error_inputs_sparse(
        _maybe_failing_sample_inputs_sparse_reduction_sum,
        _validate_sample_input_sparse_reduction,
        op_info,
        device,
        dtype,
        requires_grad,
        layout,
        **kwargs,
    )


def _validate_sample_input_sparse_default(op_info, sample, check_validate=False):
    if op_info.name == "to_sparse":
        if (
            sample.input.layout
            in {torch.sparse_csr, torch.sparse_csc, torch.sparse_bsr, torch.sparse_bsc}
            and len(sample.args) == 1
            and isinstance(sample.args[0], int)
            and sample.args[0] != 2
        ):
            sample = ErrorInput(
                sample,
                error_regex="sparse dim argument must be 2 for sparse_compressed_to_sparse",
            )

    if check_validate:
        _check_validate(op_info, sample)
    return sample


def validate_sample_input_sparse(op_info, sample, check_validate=False):
    """Return the specified sample when it is valid and supported by the
    operation. Otherwise, return the sample as ErrorInput instance.

    When check_validate is True, the result is validated against
    calling the op on the sample.
    """
    if isinstance(op_info, ReductionOpInfo):
        return _validate_sample_input_sparse_reduction(
            op_info, sample, check_validate=check_validate
        )
    else:
        return _validate_sample_input_sparse_default(
            op_info, sample, check_validate=check_validate
        )
