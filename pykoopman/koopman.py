from __future__ import annotations

from warnings import catch_warnings
from warnings import filterwarnings
from warnings import warn

import numpy as np
from numpy import empty
from numpy import vstack
from pydmd import DMD
from pydmd import DMDBase
from sklearn.base import BaseEstimator
from sklearn.metrics import r2_score
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from .common import validate_input
from .observables import Identity
from .observables import TimeDelay
from .regression import BaseRegressor
from .regression import DMDc
from .regression import EDMDc
from .regression import EnsembleBaseRegressor
from .regression import PyDMDRegressor

# from .observables import RadialBasisFunction
# from .regression import KDMD


class Koopman(BaseEstimator):
    """
    Discrete-Time Koopman class.

    The input-output data is all row-wise if stated elsewhere.
    All of the matrix, are based on column-wise linear system.

    Parameters
    ----------
    observables: observables object, optional \
            (default :class:`pykoopman.observables.Identity`)
        Map(s) to apply to raw measurement data before estimating the
        Koopman operator.
        Must extend :class:`pykoopman.observables.BaseObservables`.
        The default option, :class:`pykoopman.observables.Identity` leaves
        the input untouched.

    regressor: regressor object, optional (default ``DMD``)
        The regressor used to learn the Koopman operator from the observables.
        ``regressor`` can either extend the
        :class:`pykoopman.regression.BaseRegressor`, or ``pydmd.DMDBase``.
        In the latter case, the pydmd object must have both a ``fit``
        and a ``predict`` method.

    quiet: booolean, optional (default False)
        Whether or not warnings should be silenced during fitting.

    Attributes
    ----------
    model: sklearn.pipeline.Pipeline
        Internal representation of the forward model.
        Applies the observables and the regressor.

    n_input_features_: int
        Number of input features before computing observables.

    n_output_features_: int
        Number of output features after computing observables.

    n_control_features_: int
        Number of control features used as input to the system.
    time: dictionary
        Time vector properties.
    """

    def __init__(self, observables=None, regressor=None, quiet=False):
        if observables is None:
            observables = Identity()
        if regressor is None:
            regressor = PyDMDRegressor(DMD(svd_rank=2))  # default svd rank 2
        if isinstance(regressor, DMDBase):
            regressor = PyDMDRegressor(regressor)
        elif not isinstance(regressor, (BaseRegressor)):
            raise TypeError("Regressor must be from valid class")

        self.observables = observables
        self.regressor = regressor
        self.quiet = quiet

    def fit(self, x, y=None, u=None, dt=1):
        """
        Fit the Koopman model by learning an approximate Koopman operator.

        Parameters
        ----------
        x: numpy.ndarray, shape (n_samples, n_features)
            Measurement data to be fit. Each row should correspond to an example
            and each column a feature. If only x is provided, it is assumed that
            examples are equi-spaced in time (i.e. a uniform timestep is assumed).

        y: numpy.ndarray, shape (n_samples, n_features), (default=None)
            Target measurement data to be fit, i.e. it is assumed y = fun(x). Each row
            should correspond to an example and each column a feature. The samples in
            x and y are generally not required to be consecutive and equi-spaced.

        u: numpy.ndarray, shape (n_samples, n_control_features), (default=None)
            Control/actuation/external parameter data. Each row should correspond
            to one sample and each column a control variable or feature.
            The control variable may be amplitude of an actuator or an external,
            time-varying parameter. It is assumed that samples in u occur at the
            time instances of the corresponding samples in x,
            e.g. x(t+1) = fun(x(t), u(t)).

        dt: float, (default=1)
            Time step between samples

        Returns
        -------
        self: returns a fit ``Koopman`` instance
        """
        x = validate_input(x)

        if u is None:
            self.n_control_features_ = 0
        elif not isinstance(self.regressor, DMDc) and not isinstance(
            self.regressor, EDMDc
        ):
            raise ValueError(
                "Control input u was passed, but self.regressor is not DMDc or EDMDc"
            )

        if y is None:  # or isinstance(self.regressor, PyDMDRegressor):
            # if there is only 1 trajectory OR regressor is PyDMD
            regressor = self.regressor
        else:
            # multiple traj
            regressor = EnsembleBaseRegressor(
                regressor=self.regressor,
                func=self.observables.transform,
                inverse_func=self.observables.inverse,
            )

        steps = [
            ("observables", self.observables),
            ("regressor", regressor),
        ]
        self.model = Pipeline(steps)

        action = "ignore" if self.quiet else "default"
        with catch_warnings():
            filterwarnings(action, category=UserWarning)
            if u is None:
                self.model.fit(x, y, regressor__dt=dt)
            else:
                self.model.fit(x, y, regressor__u=u, regressor__dt=dt)
            if isinstance(self.model.steps[1][1], EnsembleBaseRegressor):
                self.model.steps[1] = (
                    self.model.steps[1][0],
                    self.model.steps[1][1].regressor_,
                )

        self.n_input_features_ = self.model.steps[0][1].n_input_features_
        self.n_output_features_ = self.model.steps[0][1].n_output_features_
        if hasattr(self.model.steps[1][1], "n_control_features_"):
            self.n_control_features_ = self.model.steps[1][1].n_control_features_

        if y is None:
            if hasattr(self.observables, "n_consumed_samples"):
                g0 = self.observables.transform(
                    x[0 : 1 + self.observables.n_consumed_samples]
                )
            else:
                g0 = self.observables.transform(x[0:1])
            self._amplitudes = np.abs(self.compute_eigen_phi_column(g0))
        else:
            self._amplitudes = None

        self.time = dict(
            [
                ("tstart", 0),
                ("tend", dt * (self.model.steps[1][1].n_samples_ - 1)),
                ("dt", dt),
            ]
        )
        return self

    def predict(self, x, u=None):
        """
        Predict the state one timestep in the future.

        Parameters
        ----------
        x: numpy.ndarray, shape (n_samples, n_input_features)
            Current state.

        u: numpy.ndarray, shape (n_samples, n_control_features), \
                optional (default None)
            Time series of external actuation/control.

        Returns
        -------
        y: numpy.ndarray, shape (n_samples, n_input_features)
            Predicted state one timestep in the future.
        """

        check_is_fitted(self, "n_output_features_")
        return self.observables.inverse(self._step(x, u))

    def simulate(self, x0, u=None, n_steps=1):
        """
        Simulate an initial state forward in time with the learned Koopman model.

        Parameters
        ----------
        x0: numpy.ndarray, shape (n_input_features,) or \
                (n_consumed_samples + 1, n_input_features)
            Initial state from which to simulate.
            If using :code:`TimeDelay` observables, ``x0`` should contain
            enough examples to compute all required time delays,
            i.e. ``n_consumed_samples + 1``.

        u: numpy.ndarray, shape (n_samples, n_control_features), \
                optional (default None)
            Time series of external actuation/control.

        n_steps: int, optional (default 1)
            Number of forward steps to be simulated.

        Returns
        -------
        y: numpy.ndarray, shape (n_steps, n_input_features)
            Simulated states.
            Note that ``y[0, :]`` is one timestep ahead of ``x0``.
        """
        check_is_fitted(self, "n_output_features_")
        # Could have an option to only return the end state and not all
        # intermediate states to save memory.

        y = empty((n_steps, self.n_input_features_), dtype=self.koopman_matrix.dtype)
        if u is None:
            y[0] = self.predict(x0)
        else:
            y[0] = self.predict(x0, u[0])

        if isinstance(self.observables, TimeDelay):
            n_consumed_samples = self.observables.n_consumed_samples
            for k in range(n_consumed_samples):
                y[k + 1] = self.predict(vstack((x0[k + 1 :], y[: k + 1])))

            for k in range(n_consumed_samples, n_steps - 1):
                y[k + 1] = self.predict(y[k - n_consumed_samples : k + 1])
        else:
            if u is None:
                for k in range(n_steps - 1):
                    y[k + 1] = self.predict(y[k].reshape(1, -1))
            else:
                for k in range(n_steps - 1):
                    y[k + 1] = self.predict(
                        y[k].reshape(1, -1), u[k + 1].reshape(1, -1)
                    )

        return y

    def score(self, x, y=None, cast_as_real=True, metric=r2_score, **metric_kws):
        """
        Score the model prediction for the next timestep.

        Parameters
        ----------
        x: numpy.ndarray, shape (n_samples, n_input_features)
            State measurements.
            Each row should correspond to the system state at some point
            in time.
            If ``y`` is not passed, then it is assumed that the examples are
            equi-spaced in time and are given in sequential order.
            If ``y`` is passed, then this assumption need not hold.

        y: numpy.ndarray, shape (n_samples, n_input_features), optional \
                (default None)
            State measurements one timestep in the future.
            Each row of this array should give the corresponding row in x advanced
            forward in time by one timestep.
            If None, the rows of ``x`` are used to construct ``y``.

        cast_as_real: bool, optional (default True)
            Whether to take the real part of predictions when computing the score.
            Many Scikit-learn metrics do not support complex numbers.

        metric: callable, optional (default ``r2_score``)
            The metric function used to score the model predictions.

        metric_kws: dict, optional
            Optional parameters to pass to the metric function.

        Returns
        -------
        score: float
            Metric function value for the model predictions at the next timestep.
        """
        check_is_fitted(self, "n_output_features_")
        x = validate_input(x)

        if isinstance(self.observables, TimeDelay):
            n_consumed_samples = self.observables.n_consumed_samples

            # User may pass in too-large
            if y is not None and len(y) == len(x):
                warn(
                    f"The first {n_consumed_samples} entries of y were ignored because "
                    "TimeDelay obesrvables were used."
                )
                y = y[n_consumed_samples:]
        else:
            n_consumed_samples = 0

        if y is None:
            if cast_as_real:
                return metric(
                    x[n_consumed_samples + 1 :].real,
                    self.predict(x[:-1]).real,
                    **metric_kws,
                )
            else:
                return metric(
                    x[n_consumed_samples + 1 :], self.predict(x[:-1]), **metric_kws
                )
        else:
            if cast_as_real:
                return metric(y.real, self.predict(x).real, **metric_kws)
            else:
                return metric(y, self.predict(x), **metric_kws)

    def get_feature_names(self, input_features=None):
        """
        Get the names of the individual features constituting the observables.

        Parameters
        ----------
        input_features: list of string, length n_input_features, \
                optional (default None)
            String names for input features, if available. By default,
            the names "x0", "x1", ... ,"xn_input_features" are used.

        Returns
        -------
        output_feature_names: list of string, length n_ouput_features
            Output feature names.
        """
        check_is_fitted(self, "n_input_features_")
        return self.observables.get_feature_names(input_features=input_features)

    def _step(self, x, u=None):
        """
        Map x one timestep forward in the space of observables.

        Parameters
        ----------
        x: numpy.ndarray, shape (n_samples, n_input_features)
            State vectors to be stepped forward.

        u: numpy.ndarray, shape (n_samples, n_control_features), \
                optional (default None)
            Time series of external actuation/control.

        Returns
        -------
        X': numpy.ndarray, shape (n_samples, self.n_output_features_)
            Observables one timestep after x.
        """
        check_is_fitted(self, "n_output_features_")

        if u is None or self.n_control_features_ == 0:
            if self.n_control_features_ > 0:
                # TODO: replace with u = 0 as default
                raise TypeError(
                    "Model was fit using control variables, so u is required"
                )
            elif u is not None:
                warn(
                    "Control variables u were ignored because control variables were"
                    " not used when the model was fit"
                )
            return self.model.predict(X=x)
        else:
            if not isinstance(self.regressor, DMDc) and not isinstance(
                self.regressor, EDMDc
            ):
                raise ValueError(
                    "Control input u was passed, but self.regressor is not DMDc "
                    "or EDMDc"
                )
            return self.model.predict(X=x, u=u)

    def reduce(self, t, x, rank=None):
        """
        Provides the option to obtain a reduced-order model from the Koopman model
        """
        if not hasattr(self.regressor, "reduce"):
            raise AttributeError("regressor type does not have this option.")

        z = self.model.steps[0][1].transform(x)
        self.model.steps[-1][1].reduce(t, x, z, self.eigenvalues_continuous, rank)

    def compute_eigen_phi_column(self, z):
        """
        given z as row vector
        outputs phi as a column vector
        """
        return self.model.steps[-1][1].compute_eigen_phi(z)

    @property
    def koopman_matrix(self):
        """
        Autonomous case:
            - the Koopman matrix K satisfying g(X') = K * g(X)
            where g denotes the observables map and X' denotes x advanced
            one timestep. Note that if there has some low rank, then K is

        Controlled case with known input
            - x' = Ax + Bu,
            - returns the A

        Controlled case with unknown input:
            - x' = K [x u]^T
        """
        check_is_fitted(self, "n_output_features_")
        return self.model.steps[-1][1].coef_

    @property
    def state_transition_matrix(self):
        """
        The state transition matrix A satisfies y' = Ay or y' = Ay + Bu, respectively,
        where y = g(x) and y is a low-rank representation.
        """
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError(
                "this type of self.regressor has no state_transition_matrix"
            )

        if hasattr(self.model.steps[-1][1], "state_matrix_"):
            return self.model.steps[-1][1].state_matrix_
        else:
            return self.koopman_matrix

    @property
    def control_matrix(self):
        """
        The control matrix (or vector) B satisfies y' = Ay + Bu.
        y is the reduced system state
        """
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError("this type of self.regressor has no control_matrix")
        return self.model.steps[-1][1].control_matrix_

    @property
    def reduced_state_transition_matrix(self):
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError(
                "this type of self.regressor has no state_transition_matrix"
            )

        if hasattr(self.model.steps[-1][1], "reduced_state_matrix_"):
            return self.model.steps[-1][1].reduced_state_matrix_

    @property
    def reduced_control_matrix(self):
        """
        The control matrix (or vector) B satisfies y' = Ay + Bu.
        y is the reduced system state
        """
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError("this type of self.regressor has no control_matrix")
        return self.model.steps[-1][1].reduced_control_matrix_

    @property
    def measurement_matrix(self):
        """
        The measurement matrix (or vector) C satisfies x = Cy
        """
        check_is_fitted(self, "model")
        # if not isinstance(self.observables, RadialBasisFunction):
        #     raise ValueError("this type of self.observable has no measurement_matrix")
        return self.model.steps[0][1].measurement_matrix_

    @property
    def projection_matrix(self):
        """
        Projection matrix
            - "encoder" left-mul maps to the low-dimensional space
        """
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError("this type of self.regressor has no projection_matrix")
        return self.model.steps[-1][1].projection_matrix_

    @property
    def projection_matrix_output(self):
        """
        Output projection matrix
            - "decoder": left mul maps to the original state space
        """
        check_is_fitted(self, "model")
        if isinstance(self.regressor, DMDBase):
            raise ValueError(
                "this type of self.regressor has no projection_matrix_output"
            )
        return self.model.steps[-1][1].projection_matrix_output_

    @property
    def modes(self):
        """
        Koopman modes
        """
        check_is_fitted(self, "model")
        return self.measurement_matrix @ self.model.steps[-1][1].unnormalized_modes

    @property
    def amplitudes(self):
        check_is_fitted(self, "model")
        return self._amplitudes
        # return self.model.steps[-1][1].amplitudes_

    @property
    def eigenvalues(self):
        """
        Discrete-time Koopman eigenvalues obtained from spectral decomposition
        of the Koopman matrix
        """
        check_is_fitted(self, "model")
        return self.model.steps[-1][1].eigenvalues_

    @property
    def frequencies(self):
        """
        Oscillation frequencies of Koopman modes/eigenvectors
        """
        check_is_fitted(self, "model")
        dt = self.time["dt"]
        return np.imag(np.log(self.eigenvalues) / dt) / (2 * np.pi)
        # return self.model.steps[-1][1].frequencies_

    @property
    def eigenvalues_continuous(self):
        """
        Continuous-time Koopman eigenvalues obtained from spectral decomposition
        of the Koopman matrix
        """
        check_is_fitted(self, "model")
        dt = self.time["dt"]
        return np.log(self.eigenvalues) / dt

    @property
    def eigenfunctions(self):
        """
        Approximate Koopman eigenfunctions
        """
        check_is_fitted(self, "model")
        return self.model.steps[-1][1].kef_

    def compute_eigenfunction(self, x):
        """
        computes the eigenfunction with row-wise output

        """
        z = self.observables.transform(x)
        phi = self.compute_eigen_phi_column(z)
        return phi.T

    def integrate_eigenfunction(self, t, x0):
        omega = self.eigenvalues_continuous
        phi0 = self.compute_eigenfunction(x0).T
        phit = []
        for i in range(len(omega)):
            phit_i = np.exp(omega[i] * t) * phi0[i]
            phit.append(phit_i)
        return np.vstack(phit).T  # (n_samples, #_eigenmodes)

    def validity_check(self, t, x):
        """
        Validity check (i.e. linearity check ) of
        eigenfunctions phi(x(t)) == phi(x(0))*exp(lambda*t)
        """

        phi = self.compute_eigenfunction(x)
        omega = self.eigenvalues_continuous
        linearity_error = []
        for i in range(len(self.eigenvalues)):
            linearity_error.append(
                np.linalg.norm(
                    # np.real(phi[i, :]) - np.real(np.exp(omega[i] * t) * (phi[i, 0:1]))
                    phi[:, i]
                    - np.exp(omega[i] * t) * phi[0:1, i]
                )
            )

        sort_idx = np.argsort(linearity_error)
        efun_index = np.arange(len(linearity_error))[sort_idx]
        linearity_error = [linearity_error[i] for i in sort_idx]
        return efun_index, linearity_error
