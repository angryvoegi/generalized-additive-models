#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Feb 27 17:58:10 2023

@author: tommy


https://arxiv.org/pdf/1809.10632.pdf
"""

import copy
from numbers import Real

import matplotlib.pyplot as plt
import numpy as np
from sklearn.utils import Bunch, check_scalar
from sklearn.utils.validation import check_is_fitted

from generalized_additive_models import GAM
from generalized_additive_models.terms import Categorical, Intercept, Linear, Spline, Tensor, Term
from generalized_additive_models.utils import cartesian


def generate_X_grid(gam, term, X, *, extrapolation=0.01, num=100, meshgrid=True):
    if not isinstance(gam, GAM):
        raise TypeError(f"Parameter `gam` must be instance of GAM, found: {gam}")

    if not isinstance(term, Term):
        raise TypeError(f"Parameter `term` must be instance of Term, found: {term}")

    check_is_fitted(gam, attributes=["coef_"])
    check_is_fitted(term)

    if isinstance(term, Intercept):
        return np.array([[1.0]])

    if isinstance(term, (Linear, Spline)):
        X_data = term._get_column(X, selector="feature")

        min_, max_ = np.min(X_data), np.max(X_data)
        offset = (max_ - min_) * extrapolation
        return np.linspace(min_ - offset, max_ + offset, num=num).reshape(-1, 1)

    if isinstance(term, Categorical):
        return np.array(term.categories_).reshape(-1, 1)

    if isinstance(term, Tensor):
        linspaces = [generate_X_grid(gam, spline, X, extrapolation=extrapolation, num=num).ravel() for spline in term]

        # Return a cartesian grid (for predicting) and a meshgrid (for plotting)
        return (cartesian(linspaces), np.meshgrid(*linspaces, indexing="ij"))


def partial_effect(gam, term, standard_deviations=1.0, edges=None, linear_scale=True):
    """

    1 standard deviation  => 0.6827 coverage
    2 standard deviations => 0.9545 coverage
    3 standard deviations => 0.9973 coverage
    4 standard deviations => 0.9999 coverage

    Coverage of 1 standard deviation is given by:

    >>> from scipy.stats import norm
    >>> float(norm().cdf(1) - norm().cdf(-1))
    0.6826894921370...

    Parameters
    ----------
    term : Linear, Spline, str, int
        A term from the fitted gam (access via gam.terms), or a feature name
        corresponding to a term from the fitted gam.
    standard_deviations : float, optional
        The number of standard deviations to cover in the credible interval.
        The default is 1.0.

    Returns
    -------
    Bunch
        A dict-like object with results.

    Examples
    --------
    >>> rng = np.random.default_rng(42)
    >>> X = rng.normal(size=(100, 1))
    >>> y = np.sin(X).ravel() + rng.normal(scale=0.1, size=100)
    >>> spline = Spline(0)
    >>> gam = GAM(spline).fit(X, y)
    >>> results = partial_effect(gam, spline)

    Or loop through the model terms like so:

    >>> for term in gam.terms:
    ...     if isinstance(term, Intercept):
    ...         continue
    ...     results = partial_effect(gam, spline)
    ...     # Plotting code goes here

    """

    # ======================= PARAMETER VALIDATION ============================
    if not isinstance(gam, GAM):
        raise TypeError(f"Parameter `gam` must be instance of GAM, found: {gam}")

    if not isinstance(term, (Spline, Linear)):
        raise TypeError(f"Parameter `term` must be instance of Spline or Linear, found: {term}")

    check_is_fitted(gam, attributes=["coef_"])
    check_is_fitted(term)

    if term not in gam.terms:
        raise ValueError(f"Term not found in model: {term}")

    standard_deviations = check_scalar(
        standard_deviations,
        "standard_deviations",
        target_type=Real,
        min_val=0,
        include_boundaries="neither",
    )

    # ================================ LOGIC  =================================

    # Get data related to term and create a smooth grid
    term = copy.deepcopy(term)  # Copy so feature_ is not changed by term.transform() below
    data = term._get_column(gam.X_, selector="feature")

    X_original_transformed = term.transform(gam.X_)

    meshgrid = None
    if isinstance(term, Tensor):
        X_smooth, meshgrid = generate_X_grid(gam, term, gam.X_, extrapolation=0.01, num=100)
        for i, spline in enumerate(term):
            spline.set_params(feature=i)
    else:
        X_smooth = generate_X_grid(gam, term, gam.X_, extrapolation=0.01, num=100)
        term.set_params(feature=0)

    # Predict on smooth grid
    X = term.transform(X_smooth)
    linear_predictions = X @ term.coef_
    predictions = linear_predictions  # if linear_scale else gam._link.inverse_link(linear_predictions)

    # Get the covariance matrix associated with the coefficients of this term
    V = gam.results_.covariance[np.ix_(term.coef_idx_, term.coef_idx_)]

    # The variance of y = X @ \beta is given by diag(X @ V @ X.T)
    # Page 293 in Wood, or see: https://math.stackexchange.com/a/2365257
    # Since we only need the diagonal, we don't form the full matrix product
    # Also, see equation (375) in The Matrix Cookbook
    # https://www.math.uwaterloo.ca/~hwolkowi/matrixcookbook.pdf

    stdev_array = np.sqrt(np.sum((X @ V) * X, axis=1))
    assert np.all(stdev_array > 0)
    assert np.allclose(stdev_array**2, np.diag(X @ V @ X.T))

    # For partial residual plots
    # https://en.wikipedia.org/wiki/Partial_residual_plot#Definition
    residuals = gam.y_ - gam._link.inverse_link(gam.model_matrix_ @ gam.coef_)

    # Prepare the linear results
    result = Bunch(
        x=X_smooth,
        y=predictions,
        y_low=predictions - standard_deviations * stdev_array,
        y_high=predictions + standard_deviations * stdev_array,
        x_obs=data,
        y_partial_residuals=(X_original_transformed @ term.coef_) + residuals,
        meshgrid=meshgrid,
    )

    # Map the variables
    if not linear_scale:
        result.y = gam._link.inverse_link(result.y)
        result.y_low = gam._link.inverse_link(result.y_low)
        result.y_high = gam._link.inverse_link(result.y_high)
        model_predictions = gam._link.inverse_link(X_original_transformed @ term.coef_)
        result.y_partial_residuals = (model_predictions + residuals,)

    return result


class PartialEffectDisplay:
    """Partial Effect visualization for smooth terms.

    It is recommend to use
    :func:`~generalized_additive_models.inspection.PartialEffectDisplay.from_estimator`
    to create a :class:`~generalized_additive_models.inspection.PartialEffectDisplay`. All parameters are
    stored as attributes.
    """

    # Partial Residual Plots in Generalized Linear Models
    # R. Dennis Cook and Rodney Croos-Dabrera
    # https://www.jstor.org/stable/2670123

    def __init__(self, *, x, y, y_low=None, y_high=None, x_obs=None, y_partial_residuals=None):
        self.x = x
        self.y = y
        self.y_low = y_low
        self.y_high = y_high
        self.x_obs = x_obs
        self.y_partial_residuals = y_partial_residuals

    def plot(
        self,
        ax=None,
        *,
        line_kwargs=None,
        rug=False,
    ):
        if line_kwargs is None:
            line_kwargs = {}

        default_line_kwargs = {"color": "black", "alpha": 0.7, "linestyle": "-"}

        line_kwargs = {**default_line_kwargs, **line_kwargs}

        if ax is None:
            _, ax = plt.subplots()

        self.line_ = ax.plot(self.x, self.y, zorder=10, **line_kwargs)[0]

        if self.y_low is not None and self.y_high is not None:
            self.fill_between_ = ax.fill_between(self.x, self.y_low, self.y_high, alpha=0.5, zorder=15)

        if rug:
            # min_y = np.min(self.y) if self.y_low is not None else np.min(self.y_low)
            min_y = ax.get_ylim()[0]
            self.scatter_ = ax.scatter(
                self.x_obs,
                np.ones_like(self.x_obs) * min_y,
                marker="|",
                color="black",
                alpha=0.7,
            )

        if self.y_partial_residuals is not None:
            self.y_partial_residuals_ = ax.scatter(self.x_obs, self.y_partial_residuals, zorder=5, s=5)

        self.ax_ = ax
        self.figure_ = ax.figure

        return self

    @classmethod
    def from_estimator(
        cls,
        gam,
        term,
        X,
        y,
        *,
        standard_deviations=1.0,
        edges=None,
        transformation=None,
        rug=False,
        residuals=False,
        ax=None,
        line_kwargs=None,
    ):
        # ======================= PARAMETER VALIDATION ============================
        if not isinstance(gam, GAM):
            raise TypeError(f"Parameter `gam` must be instance of GAM, found: {gam}")

        if not isinstance(term, Term):
            raise TypeError(f"Parameter `term` must be instance of Term, found: {term}")

        check_is_fitted(gam, attributes=["coef_"])
        check_is_fitted(term)

        if term not in gam.terms:
            raise ValueError(f"Term not found in model: {term}")

        standard_deviations = check_scalar(
            standard_deviations,
            "standard_deviations",
            target_type=Real,
            min_val=0,
            include_boundaries="neither",
        )

        # ================================ LOGIC  =================================

        # Get data related to term and create a smooth grid
        term = copy.deepcopy(term)  # Copy so feature_ is not changed by term.transform() below

        X_transformed = term.transform(X)
        x_obs = term._get_column(X, selector="feature")

        meshgrid = None
        if isinstance(term, Tensor):
            X_smooth, meshgrid = generate_X_grid(gam, term, gam.X_, extrapolation=0.01, num=2**8)
            for i, spline in enumerate(term):
                spline.set_params(feature=i)
        else:
            X_smooth = generate_X_grid(gam, term, gam.X_, extrapolation=0.01, num=2**8)
            term.set_params(feature=0)

        # Predict on smooth grid
        X_smooth_transformed = term.transform(X_smooth)
        X_smooth_transformed = term.transform(X_smooth)
        linear_predictions = X_smooth_transformed @ term.coef_

        # Get the covariance matrix associated with the coefficients of this term
        V = gam.results_.covariance[np.ix_(term.coef_idx_, term.coef_idx_)]

        # The variance of y = X @ \beta is given by diag(X @ V @ X.T)
        # Page 293 in Wood, or see: https://math.stackexchange.com/a/2365257
        # Since we only need the diagonal, we don't form the full matrix productrid()
        # Also, see equation (375) in The Matrix Cookbook
        # https://www.math.uwaterloo.ca/~hwolkowi/matrixcookbook.pdf

        stdev_array = np.sqrt(np.sum((X_smooth_transformed @ V) * X_smooth_transformed, axis=1))
        assert np.all(stdev_array > 0)
        assert np.allclose(stdev_array**2, np.diag(X_smooth_transformed @ V @ X_smooth_transformed.T))

        # Get the inverse link from the gam object, mapping from linear
        # predictions to the transformed space
        if transformation is True:
            transformation = gam._link.inverse_link
        elif transformation in (None, False):

            def transformation(x):
                return x

        # For partial residual plots
        # https://en.wikipedia.org/wiki/Partial_residual_plot#Definition

        # Transform y so it does not hit the boundary
        epsilon = np.sqrt(np.finfo(float).eps)
        low, high = gam._link.domain
        y_noboundary = np.minimum(np.maximum(y, low + epsilon), high - epsilon)
        res = gam._link.link(y_noboundary) - gam._link.link(gam.predict(X))

        y_partial_residuals = transformation(X_transformed @ term.coef_ + res)

        viz = cls(
            x=np.squeeze(X_smooth),
            y=transformation(linear_predictions),
            y_low=transformation(linear_predictions - standard_deviations * stdev_array),
            y_high=transformation(linear_predictions + standard_deviations * stdev_array),
            x_obs=x_obs,
            y_partial_residuals=y_partial_residuals if residuals else None,
        )

        return viz.plot(ax=ax, line_kwargs=line_kwargs, rug=rug)
