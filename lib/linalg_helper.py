#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

import sys
import tempfile
from functools import reduce
import numpy
import scipy.linalg
import h5py
from pyscf.lib import logger
from pyscf.lib import numpy_helper

'''
Extension to scipy.linalg module
'''

def safe_eigh(h, s, lindep=1e-15):
    '''Solve generalized eigenvalue problem  h v = w s v.

    .. note::
        The number of eigenvalues and eigenvectors might be less than the
        matrix dimension if linear dependency is found in metric s.

    Args:
        h, s : 2D array
            Complex Hermitian or real symmetric matrix.

    Kwargs:
        lindep : float
            Linear dependency threshold.  By diagonalizing the metric s, we
            consider the eigenvectors are linearly dependent subsets if their
            eigenvalues are smaller than this threshold.

    Returns:
        w, v, seig.  w is the eigenvalue vector; v is the eigenfunction array;
        seig is the eigenvalue vector of the metric s.
    '''
    seig, t = scipy.linalg.eigh(s)
    if seig[0] < lindep:
        idx = seig >= lindep
        t = t[:,idx] * (1/numpy.sqrt(seig[idx]))
        heff = reduce(numpy.dot, (t.T.conj(), h, t))
        w, v = scipy.linalg.eigh(heff)
        v = numpy.dot(t, v)
    else:
        w, v = scipy.linalg.eigh(h, s)
    return w, v, seig

# default max_memory 2000 MB
def davidson(aop, x0, precond, tol=1e-14, max_cycle=50, max_space=12,
             lindep=1e-14, max_memory=2000, dot=numpy.dot, callback=None,
             nroots=1, lessio=False, verbose=logger.WARN):
    '''Davidson diagonalization method to solve  a c = e c.  Ref
    [1] E.R. Davidson, J. Comput. Phys. 17 (1), 87-94 (1975).
    [2] http://people.inf.ethz.ch/arbenz/ewp/Lnotes/chapter11.pdf

    Args:
        aop : function(x) => array_like_x
            aop(x) to mimic the matrix vector multiplication :math:`\sum_{j}a_{ij}*x_j`.
            The argument is a 1D array.  The returned value is a 1D array.
        x0 : 1D array
            Initial guess
        precond : function(dx, e, x0) => array_like_dx
            Preconditioner to generate new trial vector.
            The argument dx is a residual vector ``a*x0-e*x0``; e is the current
            eigenvalue; x0 is the current eigenvector.

    Kwargs:
        tol : float
            Convergence tolerance.
        max_cycle : int
            max number of iterations.
        max_space : int
            space size to hold trial vectors.
        lindep : float
            Linear dependency threshold.  The function is terminated when the
            smallest eigenvalue of the metric of the trial vectors is lower
            than this threshold.
        max_memory : int or float
            Allowed memory in MB.
        dot : function(x, y) => scalar
            Inner product
        callback : function(envs_dict) => None
            callback function takes one dict as the argument which is
            generated by the builtin function :func:`locals`, so that the
            callback function can access all local variables in the current
            envrionment.
        nroots : int
            Number of eigenvalues to be computed.  When nroots > 1, it affects
            the shape of the return value
        lessio : bool
            How to compute a*x0 for current eigenvector x0.  There are two
            ways to compute a*x0.  One is to assemble the existed a*x.  The
            other is to call aop(x0).  The default is the first method which
            needs more IO and less computational cost.  When IO is slow, the
            second method can be considered.

    Returns:
        e : float or list of floats
            Eigenvalue.  By default it's one float number.  If :attr:`nroots` > 1, it
            is a list of floats for the lowest :attr:`nroots` eigenvalues.
        c : 1D array or list of 1D arrays
            Eigenvector.  By default it's a 1D array.  If :attr:`nroots` > 1, it
            is a list of arrays for the lowest :attr:`nroots` eigenvectors.

    Examples:

    >>> from pyscf import lib
    >>> a = numpy.random.random((10,10))
    >>> a = a + a.T
    >>> aop = lambda x: numpy.dot(a,x)
    >>> precond = lambda dx, e, x0: dx/(a.diagonal()-e)
    >>> x0 = a[0]
    >>> e, c = lib.davidson(aop, x0, precond, nroots=2)
    >>> len(e)
    2
    '''
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(sys.stdout, verbose)

    def qr(xs):
        #q, r = numpy.linalg.qr(numpy.asarray(xs).T)
        #q = [qi/numpy_helper.norm(qi)
        #     for i, qi in enumerate(q.T) if r[i,i] > 1e-7]
        qs = [xs[0]/numpy_helper.norm(xs[0])]
        for i in range(1, len(xs)):
            xi = xs[i].copy()
            for j in range(len(qs)):
                xi -= qs[j] * numpy.dot(qs[j], xi)
            norm = numpy_helper.norm(xi)
            if norm > 1e-7:
                qs.append(xi/norm)
        return qs

    if isinstance(x0, numpy.ndarray) and x0.ndim == 1:
        xt = [x0]
        axt = [aop(x0)]
        max_cycle = min(max_cycle,x0.size)
    else:
        xt = qr(x0)
        axt = [aop(xi) for xi in xt]
        max_cycle = min(max_cycle,x0[0].size)

    max_space = max_space + nroots * 2
    if max_memory*1e6/xt[0].nbytes/2 > max_space+nroots*2:
        xs = []
        ax = []
        _incore = True
    else:
        xs = _Xlist()
        ax = _Xlist()
        _incore = False

    toloose = numpy.sqrt(tol) * 1e-2

    for i,xi in enumerate(xt):
        xs.append(xi)
        ax.append(axt[i])
    space = len(xs)
    head = 0

    heff = numpy.empty((max_space,max_space), dtype=x0[0].dtype)
    e = 0
    for icyc in range(max_cycle):
        rnow = len(xt)
        for i in range(space):
            if head <= i < head+rnow:
                for k in range(i-head+1):
                    heff[head+k,i] = dot(xt[k].conj(), axt[i-head])
                    heff[i,head+k] = heff[head+k,i].conj()
            else:
                for k in range(rnow):
                    heff[head+k,i] = dot(xt[k].conj(), ax[i])
                    heff[i,head+k] = heff[head+k,i].conj()

        w, v = scipy.linalg.eigh(heff[:space,:space])
        try:
            de = w[:nroots] - e
        except ValueError:
            de = w[:nroots]
        e = w[:nroots]

        x0 = []
        ax0 = []
        if lessio and not _incore:
            for k, ek in enumerate(e):
                x0.append(xs[space-1] * v[space-1,k])
            for i in reversed(range(space-1)):
                xsi = xs[i]
                for k, ek in enumerate(e):
                    x0[k] += v[i,k] * xsi
            ax0 = [aop(xi) for xi in x0]
        else:
            for k, ek in enumerate(e):
                x0 .append(xs[space-1] * v[space-1,k])
                ax0.append(ax[space-1] * v[space-1,k])
            for i in reversed(range(space-1)):
                xsi = xs[i]
                axi = ax[i]
                for k, ek in enumerate(e):
                    x0 [k] += v[i,k] * xsi
                    ax0[k] += v[i,k] * axi

        ide = numpy.argmax(abs(de))
        if abs(de[ide]) < tol:
            log.debug('converge %d %d  e= %s  max|de|= %4.3g',
                      icyc, space, e, de[ide])
            break

        dx_norm = []
        xt = []
        for k, ek in enumerate(e):
            dxtmp = ax0[k] - ek * x0[k]
            xt.append(dxtmp)
            dx_norm.append(numpy_helper.norm(dxtmp))

        if max(dx_norm) < toloose:
            log.debug('converge %d %d  |r|= %4.3g  e= %s  max|de|= %4.3g',
                      icyc, space, max(dx_norm), e, de[ide])
            break

        # remove subspace linear dependency
        for k, ek in enumerate(e):
            if dx_norm[k] > toloose:
                xt[k] = precond(xt[k], e[0], x0[k])
                xt[k] *= 1/numpy_helper.norm(xt[k])
            else:
                xt[k] = None
        xt = [xi for xi in xt if xi is not None]
        for i in range(space):
            for xi in xt:
                xsi = xs[i]
                xi -= xsi * numpy.dot(xi, xsi)
        norm = 1
        for i,xi in enumerate(xt):
            norm = numpy_helper.norm(xi)
            if norm > toloose:
                xt[i] *= 1/norm
            else:
                xt[i] = None
        xt = [xi for xi in xt if xi is not None]
        if len(xt) > 1:
            xt = qr(xt)
        if len(xt) == 0:
            log.debug('Linear dependency in trial subspace')
            break
        log.debug('davidson %d %d  |r|= %4.3g  e= %s  max|de|= %4.3g  lindep= %4.3g',
                  icyc, space, max(dx_norm), e, de[ide], norm)

        if space+len(xt) > max_space:
            if _incore:
                xs = []
                ax = []
            else:
                xs = _Xlist()
                ax = _Xlist()
            xt = x0
            space = head = 0
            e = 0

        head = space
        axt = []
        for k, xi in enumerate(xt):
            if head + k >= space:
                xs.append(xt[k])
                axt.append(aop(xt[k]))
                ax.append(axt[k])
            else:
                xs[head+k] = xt[k]
                axt.append(aop(xt[k]))
                ax[head+k] = axt[k]
        space += len(xt)

        if callable(callback):
            callback(locals())

    if nroots == 1:
        return e[0], x0[0]
    else:
        return e, x0

def eigh(a, *args, **kwargs):
    if isinstance(a, numpy.ndarray) and a.ndim == 2:
        e, v = scipy.linalg.eigh(a)
        if nroots == 1:
            return e[0], v[:,0]
        else:
            return e[:nroots], v[:,:nroots].T
    else:
        return davidson(a, *args, **kwargs)
dsyev = eigh


def krylov(aop, b, x0=None, tol=1e-10, max_cycle=30, dot=numpy.dot, \
           lindep=1e-16, callback=None, hermi=False, verbose=logger.WARN):
    '''Krylov subspace method to solve  (1+a) x = b.  Ref:
    J. A. Pople et al, Int. J.  Quantum. Chem.  Symp. 13, 225 (1979).

    Args:
        aop : function(x) => array_like_x
            aop(x) to mimic the matrix vector multiplication :math:`\sum_{j}a_{ij} x_j`.
            The argument is a 1D array.  The returned value is a 1D array.

    Kwargs:
        x0 : 1D array
            Initial guess
        tol : float
            Tolerance to terminate the operation aop(x).
        max_cycle : int
            max number of iterations.
        lindep : float
            Linear dependency threshold.  The function is terminated when the
            smallest eigenvalue of the metric of the trial vectors is lower
            than this threshold.
        dot : function(x, y) => scalar
            Inner product
        callback : function(envs_dict) => None
            callback function takes one dict as the argument which is
            generated by the builtin function :func:`locals`, so that the
            callback function can access all local variables in the current
            envrionment.

    Returns:
        x : 1D array like b

    Examples:

    >>> from pyscf import lib
    >>> a = numpy.random.random((10,10)) * 1e-2
    >>> b = numpy.random.random(10)
    >>> aop = lambda x: numpy.dot(a,x)
    >>> x = lib.krylov(aop, b)
    >>> numpy.allclose(numpy.dot(a,x)+x, b)
    True
    '''
    if isinstance(aop, numpy.ndarray) and aop.ndim == 2:
        return numpy.linalg.solve(aop, b)

    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(sys.stdout, verbose)

    if x0 is None:
        xs = [b]
        ax = [aop(xs[0])]
    else:
        xs = [b-(x0 + aop(x0))]
        ax = [aop(xs[0])]

    innerprod = [dot(xs[0].conj(), xs[0])]

    h = numpy.empty((max_cycle,max_cycle), dtype=b.dtype)
    for cycle in range(max_cycle):
        x1 = ax[-1].copy()
# Schmidt orthogonalization
        for i in range(cycle+1):
            s12 = h[i,cycle] = dot(xs[i].conj(), ax[-1])        # (*)
            x1 -= (s12/innerprod[i]) * xs[i]
        h[cycle,cycle] += innerprod[cycle]                      # (*)
        innerprod.append(dot(x1.conj(), x1).real)
        log.debug('krylov cycle %d  r = %g', cycle, numpy.sqrt(innerprod[-1]))
        if innerprod[-1] < lindep:
            break
        xs.append(x1)
        ax.append(aop(x1))

        if callable(callback):
            callback(cycle, xs, ax)

    log.debug('final cycle = %d', cycle)

    nd = cycle + 1
# h = numpy.dot(xs[:nd], ax[:nd].T) + numpy.diag(innerprod[:nd])
# to reduce IO, move upper triangle (and diagonal) part to (*)
    if hermi:
        for i in range(nd):
            for j in range(i):
                h[i,j] = h[j,i].conj()
    else:
        for i in range(nd):
            for j in range(i):
                h[i,j] = dot(xs[i].conj(), ax[j])
    g = numpy.zeros(nd, dtype=b.dtype)
    g[0] = innerprod[0]
    c = numpy.linalg.solve(h[:nd,:nd], g)
    x = xs[0] * c[0]
    for i in range(1, len(c)):
        x += c[i] * xs[i]

    if x0 is not None:
        x += x0
    return x


def dsolve(aop, b, precond, tol=1e-14, max_cycle=30, dot=numpy.dot,
           lindep=1e-16, verbose=0):
    '''Davidson iteration to solve linear equation.  It works bad.
    '''

    toloose = numpy.sqrt(tol)

    xs = [precond(b)]
    ax = [aop(xs[-1])]

    aeff = numpy.zeros((max_cycle,max_cycle), dtype=b.dtype)
    beff = numpy.zeros((max_cycle), dtype=b.dtype)
    for istep in range(max_cycle):
        beff[istep] = dot(xs[istep], b)
        for i in range(istep+1):
            aeff[istep,i] = dot(xs[istep], ax[i])
            aeff[i,istep] = dot(xs[i], ax[istep])

        v = scipy.linalg.solve(aeff[:istep+1,:istep+1], beff[:istep+1])
        xtrial = dot(v, xs)
        dx = b - dot(v, ax)
        rr = numpy.linalg.norm(dx)
        if verbose:
            print('davidson', istep, rr)
        if rr < toloose:
            break
        xs.append(precond(dx))
        ax.append(aop(xs[-1]))

    if verbose:
        print(istep)

    return xtrial


class _Xlist(list):
    def __init__(self):
        self._fd = tempfile.NamedTemporaryFile()
        self.scr_h5 = h5py.File(self._fd.name, 'w')
        self.index = []
    def __del__(self):
        self.scr_h5.close()

    def __getitem__(self, n):
        key = self.index[n]
        return self.scr_h5[key].value

    def append(self, x):
        key = str(len(self.index) + 1)
        if key in self.index:
            for i in range(len(self.index)+1):
                if str(i) not in self.index:
                    key = str(i)
                    break
        self.index.append(key)
        self.scr_h5[key] = x
        self.scr_h5.flush()

    def __setitem__(self, n, x):
        key = self.index[n]
        self.scr_h5[key][:] = x
        self.scr_h5.flush()

    def __len__(self):
        return len(self.index)

    def pop(self, index):
        key = self.index.pop(index)
        del(self.scr_h5[key])


if __name__ == '__main__':
    numpy.random.seed(12)
    n = 1000
    #a = numpy.random.random((n,n))
    a = numpy.arange(n*n).reshape(n,n)
    a = numpy.sin(numpy.sin(a))
    a = a + a.T + numpy.diag(numpy.random.random(n))*10

    e,u = scipy.linalg.eigh(a)
    #a = numpy.dot(u[:,:15]*e[:15], u[:,:15].T)
    print(e[0], u[0,0])

    def aop(x):
        return numpy.dot(a, x)

    def precond(r, e0, x0):
        idx = numpy.argwhere(abs(x0)>.1).ravel()
        #idx = numpy.arange(20)
        m = idx.size
        if m > 2:
            h0 = a[idx][:,idx] - numpy.eye(m)*e0
            h0x0 = x0 / (a.diagonal() - e0)
            h0x0[idx] = numpy.linalg.solve(h0, h0x0[idx])
            h0r = r / (a.diagonal() - e0)
            h0r[idx] = numpy.linalg.solve(h0, r[idx])
            e1 = numpy.dot(x0, h0r) / numpy.dot(x0, h0x0)
            x1 = (r - e1*x0) / (a.diagonal() - e0)
            x1[idx] = numpy.linalg.solve(h0, (r-e1*x0)[idx])
            return x1
        else:
            return r / (a.diagonal() - e0)

    x0 = [a[0]/numpy.linalg.norm(a[0]),
          a[1]/numpy.linalg.norm(a[1]),
          a[2]/numpy.linalg.norm(a[2]),
          a[3]/numpy.linalg.norm(a[3])]
    e0,x0 = dsyev(aop, x0, precond, max_cycle=30, max_space=12,
                  max_memory=.0001, verbose=5, nroots=4)
    print(e0[0] - e[0])
    print(e0[1] - e[1])
    print(e0[2] - e[2])
    print(e0[3] - e[3])

##########
    a = a + numpy.diag(numpy.random.random(n)+1.1)* 10
    b = numpy.random.random(n)
    def aop(x):
        return numpy.dot(a,x)
    def precond(x, *args):
        return x / a.diagonal()
    x = numpy.linalg.solve(a, b)
    x1 = dsolve(aop, b, precond, max_cycle=50)
    print(abs(x - x1).sum())
    a_diag = a.diagonal()
    log = logger.Logger(sys.stdout, 5)
    aop = lambda x: numpy.dot(a-numpy.diag(a_diag), x)/a_diag
    x1 = krylov(aop, b/a_diag, max_cycle=50, verbose=log)
    print(abs(x - x1).sum())
    x1 = krylov(aop, b/a_diag, None, max_cycle=10, verbose=log)
    x1 = krylov(aop, b/a_diag, x1, max_cycle=30, verbose=log)
    print(abs(x - x1).sum())
