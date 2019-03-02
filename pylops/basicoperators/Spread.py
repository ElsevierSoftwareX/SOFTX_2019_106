import logging
import numpy as np
from pylops import LinearOperator

try:
    from numba import jit
except ModuleNotFoundError:
    jit = None

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.WARNING)


@jit(nopython=True, parallel=True)
def _matvec_numba(x, y, dims, interp, table, dtable):
    """numba implementation of forward mode. See official documentation for
    description of variables
    """
    x = x.reshape(dims)
    for it in range(dims[1]):
        for isp in range(dims[0]):
            indices = table[isp, it]
            if interp:
                dindices = dtable[isp, it]

            for i, indexfloat in enumerate(indices):
                index = int(indexfloat)
                if index != -9223372036854775808: # =int(np.nan)
                    if not interp:
                        y[i, index] += x[isp, it]
                    else:
                        y[i, index] += (1 -dindices[i])*x[isp, it]
                        y[i, index + 1] += dindices[i] * x[isp, it]
    return y.ravel()

@jit(nopython=True, parallel=True)
def _rmatvec_numba(x, y, dims, dimsd, interp, table, dtable):
    """numba implementation of adjoint mode. See official documentation for
    description of variables
    """
    x = x.reshape(dimsd)
    for it in range(dims[1]):
        for isp in range(dims[0]):
            indices = table[isp, it]
            if interp:
                dindices = dtable[isp, it]

            for i, indexfloat in enumerate(indices):
                index = int(indexfloat)
                if index != -9223372036854775808: # =int(np.nan)
                    if not interp:
                        y[isp, it] += x[i, index]
                    else:
                        y[isp, it] += x[i, index]*(1 - dindices[i]) + \
                                      x[i, index + 1]*dindices[i]
    return y.ravel()


class Spread(LinearOperator):
    r"""Spread operator.

    Spread values from the input model vector arranged as a 2-dimensional
    array of size :math:`[n_{sp} \times n_t]` into the data vector of size
    :math:`[n_x \times n_t]`. Spreading is performed along parametric curves
    provided as look-up table of pre-computed indices (``table``)
    or computed on-the-fly using a function handle (``fh``).

    In adjont mode, values from the data vector are instead stacked
    along the same parametric curves.

    Parameters
    ----------
    dims : :obj:`tuple`
        Dimensions of model vector (vector will be reshaped internal into
        a two-dimensional array of size :math:`[n_{sp} \times n_t]`,
        where the first dimension is the spreading/stacking direction)
    dimsd : :obj:`tuple`
        Dimensions of model vector (vector will be reshaped internal into
        a two-dimensional array of size :math:`[n_x \times n_t]`)
    table : :obj:`np.ndarray`, optional
        Look-up table of indeces of size
        :math:`[n_{sp} \times n_t \times n_x]` (if ``None`` use function
        handle ``fh``)
    dtable : :obj:`np.ndarray`, optional
        Look-up table of decimals remainders for linear interpolation of size
        :math:`[n_{sp} \times n_t \times n_x]` (if ``None`` use function
        handle ``fh``)
    fh : :obj:`np.ndarray`, optional
        Function handle that returns an index to be used for spreading/stacking
        given indices in :math:`sp` and and :math:`t`
        axes (if ``None`` use look-up table ``table``)
    engine : :obj:`str`, optional
        Engine used for fft computation (``numpy`` or ``numba``). Note that
        ``numba`` can only be used when providing a look-up table
    dtype : :obj:`str`, optional
        Type of elements in input array.

    Attributes
    ----------
    shape : :obj:`tuple`
        Operator shape
    explicit : :obj:`bool`
        Operator contains a matrix that can be solved explicitly (``True``) or
        not (``False``)

    Raises
    ------
    NotImplementedError
        If both ``table`` and ``fh`` are not provided
    ValueError
        If ``table`` has shape different from
        :math:`[n_{sp} \times n_t \times n_x]`

    Notes
    -----
    The Spread operator applies the following linear transform in forward mode
    to the model vector after reshaping it into a 2-dimensional array of size
    :math:`[n_x \times n_t]`:

    .. math::
        m(sp, t_0) \rightarrow d(x, t=f(sp, x, t_0))

    where :math:`f(sp, x, t)` is a mapping function that returns a value t
    given values :math:`sp`, :math:`x`, and  :math:`t_0`.

    In adjoint mode, the model is reconstructed by means of the following
    stacking operation:

    .. math::
        m(sp, t_0) = \int{d(x, t=f(sp, x, t_0))} dx

    Note that ``table`` (or ``fh``)  must return integer numbers
    representing indices in the axis :math:`t`. However it also possible to
    perform linear interpolation as part of the spreading/stacking process by
    providing the decimal part of the mapping function (:math:`t - \lfloor
    t \rfloor`) either in ``dtable`` input parameter or as second value in
    the return of ``fh`` function.

    """
    def __init__(self, dims, dimsd, table=None, dtable=None,
                 fh=None, engine='numpy', dtype='float64'):
        # axes
        self.dims, self.dimsd = dims, dimsd
        self.nsp, self.nt, self.nx = self.dims[0], self.dims[1], self.dimsd[0]
        self.table = table
        self.dtable = dtable
        self.fh = fh
        # find out if mapping is in table of function handle
        if table is None and fh is None:
            raise NotImplementedError('provide either table or fh...')
        elif table is not None:
            if self.table.shape != (self.nsp, self.nt, self.nx):
                raise ValueError('table must have shape [nsp x nt x nx]')
            self.usetable = True
        else:
            self.usetable = False

        # find out if linear interpolation has to be carried out
        self.interp = False
        if self.usetable:
            if dtable is not None:
                if self.dtable.shape != (self.nsp, self.nt, self.nx):
                    raise ValueError('dtable must have shape [nsp x nt x nx]')
                self.interp = True
        else:
            if len(fh(0, 0)) == 2:
                self.interp = True
        self.shape = (int(np.prod(self.dimsd)), int(np.prod(self.dims)))
        self.dtype = np.dtype(dtype)
        self.explicit = False
        if engine == 'numba' and jit is not None and self.usetable:
            self.engine = 'numba'
        else:

            if engine == 'numba' and jit is None:
                logging.warning('numba not available, revert to numpy...')
            if engine == 'numba' and not self.usetable:
                logging.warning('cannot use numba without table, '
                                'revert to numpy...')
            self.engine = 'numpy'

    def _matvec_numpy(self, x):
        x = x.reshape(self.dims)
        y = np.zeros(self.dimsd, dtype=self.dtype)
        for it in range(self.dims[1]):
            for isp in range(self.dims[0]):
                if self.usetable:
                    indices = self.table[isp, it]
                    if self.interp:
                        dindices = self.dtable[isp, it]
                else:
                    if self.interp:
                        indices, dindices = self.fh(isp, it)
                    else:
                        indices = self.fh(isp, it)
                mask = np.argwhere(~np.isnan(indices))
                if mask.size > 0:
                    indices = (indices[mask]).astype(np.int)
                    if not self.interp:
                        y[mask, indices] += x[isp, it]
                    else:
                        y[mask, indices] += (1-dindices[mask])*x[isp, it]
                        y[mask, indices + 1] += dindices[mask] * x[isp, it]
        return y.ravel()

    def _rmatvec_numpy(self, x):
        x = x.reshape(self.dimsd)
        y = np.zeros(self.dims, dtype=self.dtype)
        for it in range(self.dims[1]):
            for isp in range(self.dims[0]):
                if self.usetable:
                    indices = self.table[isp, it]
                    if self.interp:
                        dindices = self.dtable[isp, it]
                else:
                    if self.interp:
                        indices, dindices = self.fh(isp, it)
                    else:
                        indices = self.fh(isp, it)
                mask = np.argwhere(~np.isnan(indices))
                if mask.size > 0:
                    indices = (indices[mask]).astype(np.int)
                    if not self.interp:
                        y[isp, it] = np.sum(x[mask, indices])
                    else:
                        y[isp, it] = \
                            np.sum(x[mask, indices]*(1-dindices[mask])) + \
                            np.sum(x[mask, indices+1]*dindices[mask])
        return y.ravel()

    def _matvec(self, x):
        if self.engine == 'numba':
            y = np.zeros(self.dimsd, dtype=self.dtype)
            y = _matvec_numba(x, y, self.dims, self.interp,
                              self.table,
                              self.table if self.dtable is None else self.dtable)

        else:
            y = self._matvec_numpy(x)
        return y

    def _rmatvec(self, x):
        if self.engine == 'numba':
            y = np.zeros(self.dims, dtype=self.dtype)
            y = _rmatvec_numba(x, y, self.dims, self.dimsd,
                               self.interp,
                               self.table,
                               self.table if self.dtable is None else self.dtable)
        else:
            y = self._rmatvec_numpy(x)
        return y