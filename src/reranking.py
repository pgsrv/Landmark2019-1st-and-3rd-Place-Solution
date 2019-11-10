# https://github.com/fyang93/diffusion
import os
import time
from logging import getLogger

import faiss
import joblib
import numpy as np
from scipy.io import loadmat
import scipy.sparse as sparse
from scipy.sparse import csr_matrix, eye, diags
from scipy.sparse import linalg as s_linalg
from joblib import Parallel, delayed
from tqdm import tqdm

from src import utils

trunc_ids = None
trunc_init = None
lap_alpha = None
logger = getLogger('landmark')


class BaseKNN(object):
    """KNN base class"""

    def __init__(self, database, method):
        if database.dtype != np.float32:
            database = database.astype(np.float32)
        self.N = len(database)
        self.D = database[0].shape[-1]
        self.database = (
            database
            if database.flags['C_CONTIGUOUS']
            else np.ascontiguousarray(database))

    def add(self, batch_size=10000):
        """Add data into index"""
        if self.N <= batch_size:
            self.index.add(self.database)
        else:
            [self.index.add(self.database[i:i + batch_size])
             for i in tqdm(range(0, len(self.database), batch_size),
                           desc='[index] add')]

    def search(self, queries, k):
        """Search
        Args:
            queries: query vectors
            k: get top-k results
        Returns:
            sims: similarities of k-NN
            ids: indexes of k-NN
        """
        if not queries.flags['C_CONTIGUOUS']:
            queries = np.ascontiguousarray(queries)
        if queries.dtype != np.float32:
            queries = queries.astype(np.float32)
        sims, ids = self.index.search(queries, k)
        return sims, ids


class KNN(BaseKNN):
    """KNN class
    Args:
        database: feature vectors in database
        method: distance metric
    """

    def __init__(self, database, method):
        super().__init__(database, method)
        self.index = {'cosine': faiss.IndexFlatIP,
                      'euclidean': faiss.IndexFlatL2}[method](self.D)
        if os.environ.get('CUDA_VISIBLE_DEVICES'):
            print('CUDA', os.environ.get('CUDA_VISIBLE_DEVICES'))
            self.index = faiss.index_cpu_to_all_gpus(self.index)
        self.add()


class ANN(BaseKNN):
    """Approximate nearest neighbor search class
    Args:
        database: feature vectors in database
        method: distance metric
    """

    def __init__(self, database, method, M=128, nbits=8, nlist=316, nprobe=32):
        super().__init__(database, method)
        self.quantizer = {'cosine': faiss.IndexFlatIP,
                          'euclidean': faiss.IndexFlatL2}[method](self.D)
        self.index = faiss.IndexIVFPQ(self.quantizer, self.D, nlist, M, nbits)
        samples = database[np.random.permutation(np.arange(self.N))[:self.N]]
        print("[ANN] train")
        self.index.train(samples)
        self.add()
        self.index.nprobe = nprobe


def get_offline_result(i):
    ids = trunc_ids[i]
    trunc_lap = lap_alpha[ids][:, ids]
    scores, _ = linalg.cg(trunc_lap, trunc_init, tol=1e-6, maxiter=20)
    ranks = np.argsort(-scores)
    scores = scores[ranks]
    ranks = ids[ranks]
    return scores, ranks


def cache(filename):
    """Decorator to cache results
    """

    def decorator(func):
        def wrapper(*args, **kw):
            self = args[0]
            path = os.path.join(self.cache_dir, filename)
            time0 = time.time()
            if os.path.exists(path):
                result = joblib.load(path)
                cost = time.time() - time0
                logger.info(
                    '[cache] loading {} costs {:.2f}s'.format(path, cost))
                return result
            result = func(*args, **kw)
            cost = time.time() - time0
            logger.info(
                '[cache] obtaining {} costs {:.2f}s'.format(path, cost))
            joblib.dump(result, path)
            return result

        return wrapper

    return decorator


class Diffusion(object):
    """Diffusion class
    """

    def __init__(self, features, cache_dir):
        self.features = features
        self.N = len(self.features)
        self.cache_dir = cache_dir
        # use ANN for large datasets
        self.use_ann = self.N >= 100000
        if self.use_ann:
            self.ann = ANN(self.features, method='cosine')
        self.knn = KNN(self.features, method='cosine')

    # @cache('offline.jbl')
    def get_offline_results(self, n_trunc, kd=50):
        """Get offline diffusion results for each gallery feature
        """
        logger.info('[offline] starting offline diffusion')
        logger.info('[offline] 1) prepare Laplacian and initial state')
        global trunc_ids, trunc_init, lap_alpha
        if self.use_ann:
            logger.info('ann.search')
            _, trunc_ids = self.ann.search(self.features, n_trunc)
            logger.info('knn.search')
            sims, ids = self.knn.search(self.features, kd)
            logger.info('get_laplacian')
            lap_alpha = self.get_laplacian(sims, ids)
            logger.info('get_laplacian ... done')
        else:
            logger.info('knn.search')
            sims, ids = self.knn.search(self.features, n_trunc)
            trunc_ids = ids
            logger.info('get_laplacian')
            lap_alpha = self.get_laplacian(sims[:, :kd], ids[:, :kd])
            logger.info('get_laplacian ... done')
        trunc_init = np.zeros(n_trunc)
        trunc_init[0] = 1

        logger.info('[offline] 2) gallery-side diffusion')
        results = Parallel(n_jobs=-1, prefer='threads')(delayed(get_offline_result)(i)
                                                        for i in tqdm(range(self.N),
                                                                      desc='[offline] diffusion'))
        all_scores, all_ranks = map(np.concatenate, zip(*results))

        logger.info('[offline] 3) merge offline results')
        rows = np.repeat(np.arange(self.N), n_trunc)
        offline = sparse.csr_matrix((all_scores, (rows, all_ranks)),
                                    shape=(self.N, self.N),
                                    dtype=np.float32)
        return offline

    # @cache('laplacian.jbl')
    def get_laplacian(self, sims, ids, alpha=0.99):
        """Get Laplacian_alpha matrix
        """
        logger.info('get_affinity')
        affinity = self.get_affinity(sims, ids)
        logger.info('get_affinity ... done')
        num = affinity.shape[0]
        degrees = affinity @ np.ones(num) + 1e-12
        # mat: degree matrix ^ (-1/2)
        mat = sparse.dia_matrix(
            (degrees ** (-0.5), [0]), shape=(num, num), dtype=np.float32)
        logger.info('calc stochastic = mat @ affinity @ mat')
        stochastic = mat @ affinity @ mat
        sparse_eye = sparse.dia_matrix(
            (np.ones(num), [0]), shape=(num, num), dtype=np.float32)
        lap_alpha = sparse_eye - alpha * stochastic
        return lap_alpha

    # @cache('affinity.jbl')
    def get_affinity(self, sims, ids, gamma=3):
        """Create affinity matrix for the mutual kNN graph of the whole dataset
        Args:
            sims: similarities of kNN
            ids: indexes of kNN
        Returns:
            affinity: affinity matrix
        """
        num = sims.shape[0]
        sims[sims < 0] = 0  # similarity should be non-negative
        sims = sims ** gamma
        # vec_ids: feature vectors' ids
        # mut_ids: mutual (reciprocal) nearest neighbors' ids
        # mut_sims: similarites between feature vectors and their mutual nearest neighbors
        vec_ids, mut_ids, mut_sims = [], [], []
        logger.info(f'per num: {num}')
        for i in range(num):
            # check reciprocity: i is in j's kNN and j is in i's kNN
            ismutual = np.isin(ids[ids[i]], i).any(axis=1)
            if ismutual.any():
                vec_ids.append(i * np.ones(ismutual.sum()))
                mut_ids.append(ids[i, ismutual])
                mut_sims.append(sims[i, ismutual])
        logger.info('map')
        vec_ids, mut_ids, mut_sims = map(
            np.concatenate, [vec_ids, mut_ids, mut_sims])
        affinity = sparse.csc_matrix((mut_sims, (vec_ids, mut_ids)),
                                     shape=(num, num), dtype=np.float32)
        affinity[range(num), range(num)] = 0
        return affinity


def qe_dba(feats_test, feats_index, sims, topk_idx, alpha=3.0, qe=True, dba=False, n_qe=10):
    feats_concat = np.concatenate([feats_test, feats_index], axis=0)

    weights = np.expand_dims(sims[:n_qe] ** alpha, axis=-1).astype(np.float32)
    feats_concat = (feats_concat[topk_idx[:n_qe]] * weights).sum(axis=1)
    feats_concat = utils.l2norm_numpy(feats_concat.astype(np.float32))

    split_at = [len(feats_test)]
    if qe and dba:
        reranked_feats_test, reranked_feats_index = np.split(
            feats_concat, split_at, axis=0)
    elif not qe and dba:
        _, reranked_feats_index = np.split(feats_concat, split_at, axis=0)
        reranked_feats_test = feats_test
    elif qe and not dba:
        reranked_feats_test, _ = np.split(feats_concat, split_at, axis=0)
        reranked_feats_index = feats_index
    else:
        raise ValueError

    return reranked_feats_test, reranked_feats_index


def explore_exploit(q, dataset, allpair, cosine_th, param_explore_k):
    """Explore-Exploit Graph Traversal for Image Retrieval (http://www.cs.toronto.edu/~mvolkovs/cvpr2019EGT.pdf)"""

    ti_sims = allpair['ti_sims']
    ti_ids = allpair['ti_ids']
    ii_sims = allpair['ii_sims']
    ii_ids = allpair['ii_ids']

    sims, ids = ti_sims[[q.idx]], ti_ids[[q.idx]]

    V = []
    Q = []
    H = {}

    # First step: add u to V
    for i in range(param_explore_k):
        x_idx = ids[0][i]
        x_id = dataset.ids_index[x_idx]
        if x_id in Q:
            continue

        # Update score evaluation
        H[x_id] = (
            max(sims[0][i], H.get(x_id, [0])[0]),
            x_idx)

    # First step: first exploit
    candidates = list(sorted(
        [(H[k][0], k, H[k][1]) for k in H.keys()],
        key=lambda x: -x[0]))
    for score, imid, imidx in candidates:
        if len(Q) >= 100:
            break
        if score > cosine_th:
            Q.append(imid)
            V.append(imidx)
            del H[imid]
    if len(V) == 0:
        # Pop best and add it to V and Q
        score, imid, imidx = candidates[0]
        Q.append(imid)
        V.append(imidx)
        del H[imid]

    iter_count = 1
    while True:
        iter_count += 1
        # print(f'It={iter_count}, len(Q)={len(Q)}')

        assert len(V) > 0

        # Explore step
        sims, ids = ii_sims[V], ii_ids[V]

        for vidx in range(ids.shape[0]):
            for i in range(param_explore_k):
                x_idx = ids[vidx][i]
                x_id = dataset.ids_index[x_idx]
                if x_id in Q:
                    continue

                # Update score evaluation
                H[x_id] = (
                    max(sims[vidx][i], H.get(x_id, [0])[0]),
                    x_idx)

        V = []

        # Exploit step
        if len(Q) < 100:
            candidates = list(sorted([(H[k][0], k, H[k][1])
                                      for k in H.keys()],
                                     key=lambda x: -x[0]))
            for score, imid, imidx in candidates:
                if len(Q) >= 100:
                    break
                if score > cosine_th:
                    Q.append(imid)
                    V.append(imidx)
                    del H[imid]

            if len(V) == 0:
                # Pop best and add it to V and Q
                score, imid, imidx = candidates[0]
                Q.append(imid)
                V.append(imidx)
                del H[imid]

        if len(Q) >= 100:
            break
    return Q


"""
This is simple python re-implementation of the algorithms from papers Iscen.et.al "Fast Spectral Ranking for Similarity Search", CVPR2018 and Iscen et.al "Efficient Diffusion on Region Manifolds: Recovering Small Objects with Compact CNN Representations" CVPR 2017.
https://github.com/ducha-aiki/manifold-diffusion/blob/master/diffussion.py
"""


def sim_kernel(dot_product):
    return np.maximum(np.power(dot_product, 3), 0)


def normalize_connection_graph(G):
    W = csr_matrix(G)
    W = W - diags(W.diagonal())
    D = np.array(1. / np.sqrt(W.sum(axis=1)))
    D[np.isnan(D)] = 0
    D[np.isinf(D)] = 0
    D_mh = diags(D.reshape(-1))
    Wn = D_mh * W * D_mh
    return Wn


def topK_W(G, K=100):
    sortidxs = np.argsort(-G, axis=1)
    for i in range(G.shape[0]):
        G[i, sortidxs[i, K:]] = 0
    G = np.minimum(G, G.T)
    return G


def find_trunc_graph(qs, W, levels=3):
    needed_idxs = []
    needed_idxs = list(np.nonzero(qs > 0)[0])
    for l in range(levels):
        idid = W.nonzero()[1]
        needed_idxs.extend(list(idid))
        needed_idxs = list(set(needed_idxs))
    return np.array(needed_idxs), W[needed_idxs, :][:, needed_idxs]


def dfs_trunk(sim, A, alpha=0.99, QUERYKNN=10, maxiter=8, K=100, tol=1e-3):
    qsim = sim_kernel(sim).T
    sortidxs = np.argsort(-qsim, axis=1)
    for i in range(len(qsim)):
        qsim[i, sortidxs[i, QUERYKNN:]] = 0
    qsims = sim_kernel(qsim)
    W = sim_kernel(A)
    W = csr_matrix(topK_W(W, K))
    out_ranks = []
    t = time()
    for i in range(qsims.shape[0]):
        qs = qsims[i, :]
        tt = time()
        w_idxs, W_trunk = find_trunc_graph(qs, W, 2)
        Wn = normalize_connection_graph(W_trunk)
        Wnn = eye(Wn.shape[0]) - alpha * Wn
        f, inf = s_linalg.minres(Wnn, qs[w_idxs], tol=tol, maxiter=maxiter)
        ranks = w_idxs[np.argsort(-f.reshape(-1))]
        missing = np.setdiff1d(np.arange(A.shape[1]), ranks)
        out_ranks.append(np.concatenate(
            [ranks.reshape(-1, 1), missing.reshape(-1, 1)], axis=0))
    print(time() - t, 'qtime')
    out_ranks = np.concatenate(out_ranks, axis=1)
    return out_ranks


def cg_diffusion(qsims, Wn, alpha=0.99, maxiter=10, tol=1e-3):
    Wnn = eye(Wn.shape[0]) - alpha * Wn
    out_sims = []
    for i in range(qsims.shape[0]):
        #f,inf = s_linalg.cg(Wnn, qsims[i,:], tol=tol, maxiter=maxiter)
        f, inf = s_linalg.minres(Wnn, qsims[i, :], tol=tol, maxiter=maxiter)
        out_sims.append(f.reshape(-1, 1))
    out_sims = np.concatenate(out_sims, axis=1)
    ranks = np.argsort(-out_sims, axis=0)
    return ranks


def fsr_rankR(qsims, Wn, alpha=0.99, R=2000):
    vals, vecs = s_linalg.eigsh(Wn, k=R)
    p2 = diags((1.0 - alpha) / (1.0 - alpha*vals))
    vc = csr_matrix(vecs)
    p3 = vc.dot(p2)
    vc_norm = (vc.multiply(vc)).sum(axis=0)
    out_sims = []
    for i in range(qsims.shape[0]):
        qsims_sparse = csr_matrix(qsims[i:i+1, :])
        p1 = (vc.T).dot(qsims_sparse.T)
        diff_sim = csr_matrix(p3)*csr_matrix(p1)
        out_sims.append(diff_sim.todense().reshape(-1, 1))
    out_sims = np.concatenate(out_sims, axis=1)
    ranks = np.argsort(-out_sims, axis=0)
    return ranks
