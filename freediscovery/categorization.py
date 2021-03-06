# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import numpy as np
import scipy
from scipy.special import logit
from sklearn.externals import joblib
from sklearn.neighbors import NearestNeighbors, NearestCentroid
from sklearn.base import BaseEstimator
from sklearn.utils.validation import check_array


from .base import _BaseWrapper, RankerMixin
from .utils import setup_model, _rename_main_thread
from .exceptions import (ModelNotFound, WrongParameter, NotImplementedFD, OptionalDependencyMissing)


def _zip_relevant(relevant_id, non_relevant_id):
    """ Take a list of relevant and non relevant documents id and return
    an array of indices and prediction values """
    idx_id = np.hstack((np.asarray(relevant_id), np.asarray(non_relevant_id)))
    y = np.concatenate((np.ones((len(relevant_id))),
                        np.zeros((len(non_relevant_id))))).astype(np.int)
    return idx_id, y

def _unzip_relevant(idx_id, y):
    """Take an array of indices and prediction values and return
    a list of relevant and non relevant documents id

    Parameters
    ----------
    idx_id : ndarray[int] (n_samples)
        array of indices
    y : ndarray[float] (n_samples)
        target array
    """
    mask = np.asarray(y) > 0.5
    idx_id = np.asarray(idx_id, dtype='int')
    return idx_id[mask], idx_id[~mask]


def explain_binary_categorization(estimator, vocabulary, X_row):
    """Explain the binary categorization results

    Parameters
    ----------
    estimator : sklearn.base.BaseEstimator
      the binary categorization estimator
      (must have a `decision_function` method)
    vocabulary : list [n_features]
      vocabulary (list of words or n-grams) 
    X_row : sparse CSR ndarray [n_features]
      a row of the document term matrix
    """
    if X_row.ndim != 2 or X_row.shape[0] != 1:
        raise ValueError('X_row must be an 2D sparse array,'
                         'with shape (1, N) not {}'.format(X_row.shape))
    if X_row.shape[1] != len(vocabulary):
        raise ValueError(
                'The vocabulary length ({}) does not match '.format(len(vocabulary)) +\
                'the number of features in X_row ({})'.format(X_row.shape[1]))

    vocabulary_inv = {ind: key for key, ind in vocabulary.items()}


    if type(estimator).__name__ == 'LogisticRegression':
        coef_ = estimator.coef_
        if X_row.shape[1] != coef_.shape[1]:
            raise ValueError("Coefficients size {} does not match n_features={}".format(
                                        coef_.shape[1], X_row.shape[1]))

        indices = X_row.indices
        weights = X_row.data*estimator.coef_[0, indices]
        weights_dict = {}
        for ind, value in zip(indices, weights):
            key = vocabulary_inv[ind]
            weights_dict[key] = value
        return weights_dict
    else:
        raise NotImplementedError()

# a subclass of the NearestCentroid from scikit-learn that also
# includes the distance to the nearest centroid

class NearestCentroidRanker(NearestCentroid):

    def decision_function(self, X):
        """Compute the distances to the nearest centroid for
        an array of test vectors X.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
        Returns
        -------
        C : array, shape = [n_samples]
        """
        from sklearn.metrics.pairwise import pairwise_distances
        from sklearn.utils.validation import check_array, check_is_fitted

        check_is_fitted(self, 'centroids_')

        X = check_array(X, accept_sparse='csr')

        return pairwise_distances(X, self.centroids_, metric=self.metric).min(axis=1)

def _chunk_kneighbors(func, X, batch_size=5000, **args):
    """ Chunk kneighbors computations to reduce RAM requirements

    Parameters
    ----------
    func : function
      the function to run
    X : ndarray
      the array func is applied to
    batch_size : int
      batch size

    Returns
    -------
    dist : array
       distance array
    ind : array of indices
    """
    n_samples = X.shape[0]
    ind_arr = []
    dist_arr = []
    # don't enter the last loop if n_sampes is a multiple of batch_size
    for k in range(n_samples//batch_size + int(n_samples % batch_size != 0)):
        mslice = slice(k*batch_size, min((k+1)*batch_size, n_samples))
        X_sl = X[mslice, :]
        dist_k, ind_k = func(X_sl, **args)
        ind_arr.append(ind_k)
        dist_arr.append(dist_k)
    return (np.concatenate(dist_arr, axis=0),
            np.concatenate(ind_arr, axis=0))



class NearestNeighborRanker(BaseEstimator, RankerMixin):
    """A nearest neighbor ranker, behaves like
        * KNeigborsClassifier (supervised) when trained on both positive and negative samples
        * NearestNeighbors  (unsupervised) when trained on positive samples only

    Parameters
    ----------
    radius : float, optional (default = 1.0)
        Range of parameter space to use by default for :meth:`radius_neighbors`
        queries.

    algorithm : {'auto', 'ball_tree', 'kd_tree', 'brute'}, optional
        Algorithm used to compute the nearest neighbors:

        - 'ball_tree' will use :class:`BallTree`
        - 'kd_tree' will use :class:`KDtree`
        - 'brute' will use a brute-force search.
        - 'auto' will attempt to decide the most appropriate algorithm
          based on the values passed to :meth:`fit` method.

        Note: fitting on sparse input will override the setting of
        this parameter, using brute force.

    leaf_size : int, optional (default = 30)
        Leaf size passed to BallTree or KDTree.  This can affect the
        speed of the construction and query, as well as the memory
        required to store the tree.  The optimal value depends on the
        nature of the problem.

    n_jobs : int, optional (default = 1)
        The number of parallel jobs to run for neighbors search.
        If ``-1``, then the number of jobs is set to the number of CPU cores.

    method : str, def
        If "unsupervised" only distances to the positive samples are used in the ranking
        If "supervised" both the distance to the positive and negative documents are used
        for ranking (i.e. if a document is slightly further away from a positive document
        than from a negative one, it will be considered negative with a very low score)

    """

    def __init__(self, radius=1.0,
                 algorithm='brute', leaf_size=30, n_jobs=1,
                 ranking='supervised', **kwargs):

        # define nearest neighbors search objects for positive and negative samples
        self._mod_p = NearestNeighbors(n_neighbors=1,
                                       leaf_size=leaf_size,
                                       algorithm=algorithm,
                                       n_jobs=n_jobs,
                                       metric='euclidean',  # euclidean metric by default
                                       **kwargs)
        self._mod_n = NearestNeighbors(n_neighbors=1,
                                       leaf_size=leaf_size,
                                       algorithm=algorithm,
                                       n_jobs=n_jobs,
                                       metric='euclidean',  # euclidean metric by default
                                       **kwargs)
        if ranking not in ['supervised', 'unsupervised']:
            raise ValueError
        self.ranking_ = ranking

    @staticmethod
    def _ranking_score(d_p, d_n=None):
        """ Compute the ranking score from the positive an negative
        distances on L2 normalized data

        Parameters
        ----------
        d_p : array (n_samples,)
           distance to the positive samples
        d_n : array (n_samples,)
           (optional) distance to the negative samples

        Returns
        -------
        score : array (n_samples,)
           the ranking score in the range [-1, 1]
           For positive items score = 1 - cosine distance / 2
        """
        S_p = 1 - d_p
        if d_n is not None:
            S_n = 1 - d_n
            return np.where(S_p > S_n,
                            S_p + 1,
                            -1 - S_n) / 2
        else:
            return (S_p + 1) / 2

    def fit(self, X, y):
        """Fit the model using X as training data
        Parameters
        ----------
        X : {array-like, sparse matrix, BallTree, KDTree}
            Training data, shape [n_samples, n_features],

        """
        X = check_array(X, accept_sparse='csr')

        index = np.arange(X.shape[0], dtype='int')

        self._index_p, self._index_n = _unzip_relevant(index, y)


        if self._index_p.shape[0] > 0:
            self._mod_p.fit(X[self._index_p])
        else:
            raise ValueError('Training sets with no positive labels are not supported!')
        if self._index_n.shape[0] > 0:
            self._mod_n.fit(X[self._index_n])
        else:
            pass

    def kneighbors(self, X=None, batch_size=5000):
        """Finds the K-neighbors of a point.
        Returns indices of and distances to the neighbors of each point.
        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            the input array
        batch_size : int
            the batch size
        Returns
        -------
        score : array
            ranking score (based on cosine similarity)
        ind : array
            Indices of the nearest points in the population matrix.
        md : dict
            Additional result data
              * ind_p : Indices of the nearest positive points
              * ind_n : Indices of the nearest negate points
              * dist_p : distance to the nearest positive points
              * dist_n : distance to the nearest negate points
        --------
        """
        X = check_array(X, accept_sparse='csr')

        D_p, idx_p_loc = _chunk_kneighbors(self._mod_p.kneighbors, X,
                                           batch_size=batch_size)

        # only NearestNeighbor-1 (only one column in the kneighbors output)
        # convert from eucledian distance in L2 norm space to cosine similarity
        D_p = D_p[:,0] / 2
        # map local index within _index_p, _index_n to global index
        ind_p = self._index_p[idx_p_loc[:,0]]

        md = {'dist_p': D_p,
              'ind_p': ind_p,
             }

        if self._mod_n._fit_method is not None: # also corresponds to "unsupervised" method
            D_n, idx_n_loc = _chunk_kneighbors(self._mod_n.kneighbors, X,
                                               batch_size=batch_size)
            D_n = D_n[:,0] / 2
            ind_n = self._index_n[idx_n_loc[:,0]]
            md['ind_n'] = ind_n
            md['dist_n'] = D_n
            if self.ranking_ == 'supervised':
                ind = np.where(D_p <= D_n, ind_p, ind_n)
            else:
                ind = ind_p
        else:
            D_n = None
            ind = ind_p

        if self.ranking_ == 'supervised':
            score = self._ranking_score(D_p, D_n)
        elif self.ranking_ == 'unsupervised':
            score = self._ranking_score(D_p, None)
        else:
            raise ValueError


        return score, ind , md



class _CategorizerWrapper(_BaseWrapper):
    """ Document categorization model

    The option `use_hashing=True` must be set for the feature extraction.
    Recommended options also include, `use_idf=1, sublinear_tf=0, binary=0`.

    Parameters
    ----------
    cache_dir : str
      folder where the model will be saved
    parent_id : str, optional
      dataset id
    mid : str, optional
      model id
    cv_scoring : str, optional, default='roc_auc'
      score that is used for Cross Validation, cf. sklearn
    cv_n_folds : str, optional
      number of K-folds used for Cross Validation
    """

    _wrapper_type = "categorizer"

    def __init__(self, cache_dir='/tmp/',  parent_id=None, mid=None,
            cv_scoring='roc_auc', cv_n_folds=3):

        super(_CategorizerWrapper, self).__init__(cache_dir=cache_dir,
                                          parent_id=parent_id,
                                          mid=mid, load_model=True)

        self.cv_scoring = cv_scoring
        self.cv_n_folds = cv_n_folds


    @staticmethod
    def _build_estimator(Y_train, method, cv, cv_scoring, cv_n_folds, **options):
        if cv:
            #from sklearn.cross_validation import StratifiedKFold
            #cv_obj = StratifiedKFold(n_splits=cv_n_folds, shuffle=False)
            cv_obj = cv_n_folds  # temporary hack (due to piclking issues otherwise, this needs to be fixed)
        else:
            cv_obj = None

        _rename_main_thread()

        if method == 'LinearSVC':
            from sklearn.svm import LinearSVC
            if cv is None:
                cmod = LinearSVC(**options)
            else:
                try:
                    from freediscovery_extra import make_linearsvc_cv_model
                except ImportError:
                    raise OptionalDependencyMissing('freediscovery_extra')
                cmod = make_linearsvc_cv_model(cv_obj, cv_scoring, **options)
        elif method == 'LogisticRegression':
            from sklearn.linear_model import LogisticRegression
            if cv is None:
                cmod = LogisticRegression(**options)
            else:
                try:
                    from freediscovery_extra import make_logregr_cv_model
                except ImportError:
                    raise OptionalDependencyMissing('freediscovery_extra')
                cmod = make_logregr_cv_model(cv_obj, cv_scoring, **options)
        elif method == 'NearestCentroid':
            cmod  = NearestCentroidRanker()
        elif method == 'NearestNeighbor':
            cmod = NearestNeighborRanker()
        elif method == 'xgboost':
            try:
                import xgboost as xgb
            except ImportError:
                raise OptionalDependencyMissing('xgboost')
            if cv is None:
                try:
                    from freediscovery_extra import make_xgboost_model
                except ImportError:
                    raise OptionalDependencyMissing('freediscovery_extra')
                cmod = make_xgboost_model(cv_obj, cv_scoring, **options)
            else:
                try:
                    from freediscovery_extra import make_xgboost_cv_model
                except ImportError:
                    raise OptionalDependencyMissing('freediscovery_extra')
                cmod = make_xgboost_cv_model(cv, cv_obj, cv_scoring, **options)
        elif method == 'MLPClassifier':
            if cv is not None:
                raise NotImplementedFD('CV not supported with MLPClassifier')
            from sklearn.neural_network import MLPClassifier
            cmod = MLPClassifier(solver='adam', hidden_layer_sizes=10,
                                 max_iter=200, activation='identity', verbose=0)
        else:
            raise WrongParameter('Method {} not implemented!'.format(method))
        return cmod

    def train(self, index, y, method='LinearSVC', cv=None):
        """
        Train the categorization model

        Parameters
        ----------
        index : array-like, shape (n_samples)
           document indices of the training set
        y : array-like, shape (n_samples)
           target binary class relative to index
        method : str
           the ML algorithm to use (one of "LogisticRegression", "LinearSVC", 'xgboost')
        cv : str
           use cross-validation
        Returns
        -------
        cmod : sklearn.BaseEstimator
           the scikit learn classifier object
        Y_train : array-like, shape (n_samples)
           training predictions
        """

        valid_methods = ["LinearSVC", "LogisticRegression", "xgboost",
                         "NearestCentroid", "NearestNeighbor"]

        if method in ['ensemble-stacking', 'MLPClassifier']:
            raise WrongParameter('method={} is implemented but not production ready. It was disabled for now.'.format(method))

        if method not in valid_methods:
            raise WrongParameter('method={} is not supported, should be one of {}'.format(
                method, valid_methods)) 
        if cv is not None and method in ['NearestNeighbor', 'NearestCentroid']:
            raise WrongParameter('Cross validation (cv={}) not supported with {}'.format(
                                        cv, method))

        if cv not in [None, 'fast', 'full']:
            raise WrongParameter('cv')

        if method == 'ensemble-stacking':
            if cv is not None:
                raise WrongParameter('CV with ensemble stacking is not supported!')

        d_all = self.pipeline.data  #, mmap_mode='r')

        X_train = d_all[index, :]

        Y_train = y

        if method != 'ensemble-stacking':
            cmod = self._build_estimator(Y_train, method, cv, self.cv_scoring, self.cv_n_folds)
        else:
            from freediscovery.private import _EnsembleStacking

            cmod_logregr = self._build_estimator(Y_train, 'LogisticRegression', 'full',
                                             self.cv_scoring, self.cv_n_folds)
            cmod_svm = self._build_estimator(Y_train, 'LinearSVC', 'full',
                                             self.cv_scoring, self.cv_n_folds)
            cmod_xgboost = self._build_estimator(Y_train, 'xgboost', None,
                                             self.cv_scoring, self.cv_n_folds)
            cmod_xgboost.transform = cmod_xgboost.predict
            cmod = _EnsembleStacking([('logregr', cmod_logregr),
                                      ('svm', cmod_svm),
                                      ('xgboost', cmod_xgboost)
                                      ])

        mid, mid_dir = setup_model(self.model_dir)

        if method == 'xgboost' and not cv:
            cmod.fit(X_train, Y_train, eval_metric='auc')
        else:
            cmod.fit(X_train, Y_train)

        joblib.dump(cmod, os.path.join(mid_dir, 'model'), compress=9)

        pars = {
            'method': method,
            'index': index,
            'y': y
            }
        pars['options'] = cmod.get_params()
        self._pars = pars
        joblib.dump(pars, os.path.join(mid_dir, 'pars'), compress=9)

        self.mid = mid
        self.cmod = cmod
        return cmod, Y_train

    def predict(self, chunk_size=5000):
        """
        Predict the relevance using a previously trained model

        Parameters
        ----------
        chunck_size : int
           chunck size
        """

        if self.cmod is not None:
            cmod = self.cmod
        else:
            raise WrongParameter('The model must be trained first, or sid must be provided to load\
                    a previously trained model!')

        ds = self.pipeline.data

        md = {}
        if isinstance(cmod, NearestNeighborRanker):
            res, _, md = cmod.kneighbors(ds)
        elif hasattr(cmod, 'decision_function'):
            res = cmod.decision_function(ds)
        else:  # gradient boosting define the decision function by analogy
            tmp = cmod.predict_proba(ds)[:, 1]
            res = logit(tmp)
        return res, md

    def _load_pars(self, mid=None):
        """Load model parameters from disk"""
        if mid is None:
            mid = self.mid
        mid_dir = os.path.join(self.model_dir, mid)
        pars = super(_CategorizerWrapper, self)._load_pars(mid)
        cmod = joblib.load(os.path.join(mid_dir, 'model'))
        pars['options'] = cmod.get_params()
        return pars
