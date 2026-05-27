"""
Topological Data Analysis Engine
════════════════════════════════
Detects market regime transitions using persistent homology from
point-cloud embeddings of price/return data.

Key concepts:
  • Persistence Diagram - birth/death pairs of topological features (0-dim = connected components, 1-dim = loops)
  • Betti Numbers - number of live features at each filtration step
  • Persistence Entropy - information-theoretic measure of topological complexity
  • Anomaly Score - deviation from baseline topology indicating regime change

Implementation uses numpy/scipy only (no gudhi/ripser dependency for deployment).
For production, consider switching to ripser.py for performance.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class PersistenceFeature:
    """A single persistent topological feature."""
    dimension: int          # 0 = connected component, 1 = loop
    birth: float
    death: float
    persistence: float      # death - birth
    
    @property
    def is_infinite(self) -> bool:
        return self.death == float('inf') or self.death > 1e10


@dataclass
class TDAFeatureSet:
    """Complete TDA feature extraction result."""
    symbol: str
    n_bars: int
    
    # Persistence features
    features_0d: List[PersistenceFeature]    # connected components
    features_1d: List[PersistenceFeature]    # loops
    
    # Betti numbers at key filtration levels
    betti_0: List[int]     # Betti-0 at each filtration step
    betti_1: List[int]     # Betti-1 at each filtration step
    
    # Summary statistics
    n_components: int       # number of connected components (Betti-0 at full filtration)
    n_loops: int            # number of loops (Betti-1 at full filtration)
    
    # Persistence statistics
    max_persistence_0d: float
    mean_persistence_0d: float
    max_persistence_1d: float
    mean_persistence_1d: float
    
    # Persistence entropy (measures topological complexity)
    persistence_entropy_0d: float
    persistence_entropy_1d: float
    total_persistence_entropy: float
    
    # Anomaly score (deviation from baseline)
    anomaly_score: float         # 0 = normal, > 0 = anomalous topology
    regime_change_probability: float  # [0, 1] probability of regime transition
    
    # Feature vector (for ML pipeline)
    feature_vector: List[float]  # 10-dim summary: [n_comp, n_loops, max_p0, mean_p0, max_p1, mean_p1, ent0, ent1, anomaly, regime_change_p]
    feature_labels: List[str]


class TDAEngine:
    """
    Topological Data Analysis engine for financial time series.
    
    Uses a sliding window embedding (Takens) to create point clouds
    from price series, then computes persistent homology via a
    distance matrix approach.
    
    No external TDA library required - uses numpy/scipy for
    Vietoris-Rips complex computation at small scales.
    """
    
    def __init__(
        self,
        embedding_dim: int = 3,        # Takens embedding dimension
        embedding_delay: int = 1,       # time delay for embedding
        max_filtration: float = 2.0,    # max filtration value (in std units)
        n_filtration_steps: int = 20,   # filtration resolution
        anomaly_threshold: float = 1.5, # std deviations for anomaly
    ):
        self.embedding_dim = embedding_dim
        self.embedding_delay = embedding_delay
        self.max_filtration = max_filtration
        self.n_filtration_steps = n_filtration_steps
        self.anomaly_threshold = anomaly_threshold
        self._baseline_entropy = None
    
    def extract_features(
        self,
        prices: np.ndarray,
        symbol: str = "UNKNOWN",
        baseline_prices: Optional[np.ndarray] = None,
    ) -> TDAFeatureSet:
        """
        Extract TDA features from a price series.
        
        Steps:
        1. Compute returns
        2. Create Takens embedding (sliding window point cloud)
        3. Compute distance matrix
        4. Compute persistent homology (0-dim and 1-dim)
        5. Compute Betti numbers, persistence entropy, anomaly score
        """
        prices = np.asarray(prices, dtype=float)
        returns = np.diff(np.log(prices))
        returns = returns[np.isfinite(returns)]
        
        if len(returns) < 50:
            return self._empty_result(symbol, len(prices))
        
        # Standardise returns for topology
        r_std = (returns - np.mean(returns)) / (np.std(returns) + 1e-10)
        
        # Takens embedding
        point_cloud = self._takens_embedding(r_std)
        
        if len(point_cloud) < 10:
            return self._empty_result(symbol, len(prices))
        
        # Distance matrix
        dist_matrix = self._distance_matrix(point_cloud)
        
        # Compute persistence
        features_0d = self._compute_persistence_0d(dist_matrix)
        features_1d = self._compute_persistence_1d(dist_matrix)
        
        # Betti numbers
        betti_0, betti_1 = self._compute_betti_numbers(dist_matrix)
        
        # Persistence statistics
        finite_0d = [f for f in features_0d if not f.is_infinite]
        finite_1d = [f for f in features_1d if not f.is_infinite]
        
        max_p0 = max((f.persistence for f in finite_0d), default=0.0)
        mean_p0 = float(np.mean([f.persistence for f in finite_0d])) if finite_0d else 0.0
        max_p1 = max((f.persistence for f in finite_1d), default=0.0)
        mean_p1 = float(np.mean([f.persistence for f in finite_1d])) if finite_1d else 0.0
        
        # Persistence entropy
        ent_0d = self._persistence_entropy(finite_0d)
        ent_1d = self._persistence_entropy(finite_1d)
        total_ent = ent_0d + ent_1d
        
        # Compute baseline if provided
        if baseline_prices is not None:
            baseline_result = self.extract_features(baseline_prices, symbol + "_baseline")
            self._baseline_entropy = baseline_result.total_persistence_entropy
        
        # Anomaly score
        anomaly = self._compute_anomaly_score(total_ent)
        regime_change_p = self._regime_change_probability(anomaly, betti_0, betti_1)
        
        # Feature vector
        feature_vector = [
            len(finite_0d),       # n_components
            len(finite_1d),       # n_loops
            max_p0,               # max persistence 0d
            mean_p0,              # mean persistence 0d
            max_p1,               # max persistence 1d
            mean_p1,              # mean persistence 1d
            ent_0d,               # entropy 0d
            ent_1d,               # entropy 1d
            anomaly,              # anomaly score
            regime_change_p,      # regime change probability
        ]
        feature_labels = [
            "n_components", "n_loops", "max_persistence_0d", "mean_persistence_0d",
            "max_persistence_1d", "mean_persistence_1d", "entropy_0d", "entropy_1d",
            "anomaly_score", "regime_change_probability",
        ]
        
        return TDAFeatureSet(
            symbol=symbol,
            n_bars=len(prices),
            features_0d=features_0d,
            features_1d=features_1d,
            betti_0=betti_0,
            betti_1=betti_1,
            n_components=len(finite_0d),
            n_loops=len(finite_1d),
            max_persistence_0d=round(max_p0, 6),
            mean_persistence_0d=round(mean_p0, 6),
            max_persistence_1d=round(max_p1, 6),
            mean_persistence_1d=round(mean_p1, 6),
            persistence_entropy_0d=round(ent_0d, 6),
            persistence_entropy_1d=round(ent_1d, 6),
            total_persistence_entropy=round(total_ent, 6),
            anomaly_score=round(anomaly, 6),
            regime_change_probability=round(regime_change_p, 6),
            feature_vector=[round(x, 6) for x in feature_vector],
            feature_labels=feature_labels,
        )
    
    def _takens_embedding(self, series: np.ndarray) -> np.ndarray:
        """Create a Takens time-delay embedding."""
        d = self.embedding_dim
        tau = self.embedding_delay
        n = len(series)
        
        if n < d * tau:
            return series.reshape(-1, 1)
        
        m = n - (d - 1) * tau
        embedded = np.zeros((m, d))
        for i in range(m):
            for j in range(d):
                embedded[i, j] = series[i + j * tau]
        
        return embedded
    
    def _distance_matrix(self, points: np.ndarray) -> np.ndarray:
        """Compute pairwise Euclidean distance matrix."""
        n = len(points)
        # Cap at 200 points for performance
        if n > 200:
            indices = np.linspace(0, n - 1, 200, dtype=int)
            points = points[indices]
            n = 200
        
        diff = points[:, np.newaxis, :] - points[np.newaxis, :, :]
        dist = np.sqrt(np.sum(diff ** 2, axis=2))
        return dist
    
    def _compute_persistence_0d(self, dist_matrix: np.ndarray) -> List[PersistenceFeature]:
        """
        Compute 0-dimensional persistent homology (connected components).
        
        Uses Union-Find on sorted edges to compute birth/death of components.
        """
        n = dist_matrix.shape[0]
        if n < 2:
            return [PersistenceFeature(0, 0.0, float('inf'), float('inf'))]
        
        # Get upper triangle edges sorted by distance
        edges = []
        for i in range(n):
            for j in range(i + 1, n):
                edges.append((dist_matrix[i, j], i, j))
        edges.sort(key=lambda x: x[0])
        
        # Union-Find
        parent = list(range(n))
        rank = [0] * n
        
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        
        def union(x, y):
            rx, ry = find(x), find(y)
            if rx == ry:
                return False
            if rank[rx] < rank[ry]:
                rx, ry = ry, rx
            parent[ry] = rx
            if rank[rx] == rank[ry]:
                rank[rx] += 1
            return True
        
        # Each point starts as its own component (birth = 0)
        # When two components merge, the younger one dies
        component_birth = {i: 0.0 for i in range(n)}
        features = []
        n_alive = n
        
        for dist_val, i, j in edges:
            if dist_val > self.max_filtration:
                break
            ri, rj = find(i), find(j)
            if ri != rj:
                # Merge: one component dies
                birth_i = component_birth.get(ri, 0.0)
                birth_j = component_birth.get(rj, 0.0)
                
                # The younger component dies
                if birth_i <= birth_j:
                    death_birth = birth_j
                    death_dist = dist_val
                    # Keep ri as the surviving root
                    union(i, j)
                    new_root = find(i)
                    component_birth[new_root] = birth_i
                else:
                    death_birth = birth_i
                    death_dist = dist_val
                    union(i, j)
                    new_root = find(i)
                    component_birth[new_root] = birth_j
                
                persistence = death_dist - death_birth
                features.append(PersistenceFeature(
                    dimension=0,
                    birth=round(death_birth, 6),
                    death=round(death_dist, 6),
                    persistence=round(persistence, 6),
                ))
                n_alive -= 1
                
                if n_alive <= 1:
                    break
        
        # The last surviving component has infinite persistence
        features.append(PersistenceFeature(
            dimension=0, birth=0.0, death=float('inf'), persistence=float('inf')
        ))
        
        return features
    
    def _compute_persistence_1d(self, dist_matrix: np.ndarray) -> List[PersistenceFeature]:
        """
        Compute 1-dimensional persistent homology (loops).
        
        Simplified approach: detect loops from the distance matrix
        by finding triplets (triangles) with small perimeter.
        A loop is born when a triangle is completed and dies when
        it is filled in.
        
        This is a simplified approximation - for production use ripser.
        """
        n = dist_matrix.shape[0]
        if n < 3:
            return []
        
        # Find triangles with small perimeter
        triangles = []
        threshold = self.max_filtration * 0.5
        
        for i in range(min(n, 50)):  # limit for performance
            for j in range(i + 1, min(n, 50)):
                if dist_matrix[i, j] > threshold:
                    continue
                for k in range(j + 1, min(n, 50)):
                    if dist_matrix[i, k] > threshold or dist_matrix[j, k] > threshold:
                        continue
                    perimeter = dist_matrix[i, j] + dist_matrix[j, k] + dist_matrix[i, k]
                    max_edge = max(dist_matrix[i, j], dist_matrix[j, k], dist_matrix[i, k])
                    triangles.append((max_edge, perimeter / 3.0, perimeter))
        
        if not triangles:
            return []
        
        triangles.sort(key=lambda x: x[0])
        
        features = []
        for birth, death_approx, _ in triangles[:20]:  # limit features
            persistence = death_approx - birth
            if persistence > 0.001:
                features.append(PersistenceFeature(
                    dimension=1,
                    birth=round(birth, 6),
                    death=round(death_approx, 6),
                    persistence=round(persistence, 6),
                ))
        
        return features
    
    def _compute_betti_numbers(self, dist_matrix: np.ndarray) -> tuple:
        """Compute Betti numbers at each filtration step."""
        n = dist_matrix.shape[0]
        filtration_steps = np.linspace(0, self.max_filtration, self.n_filtration_steps)
        
        betti_0 = []
        betti_1 = []
        
        for eps in filtration_steps:
            # Betti-0: number of connected components
            visited = [False] * n
            components = 0
            for i in range(n):
                if not visited[i]:
                    # BFS
                    queue = [i]
                    visited[i] = True
                    while queue:
                        node = queue.pop(0)
                        for j in range(n):
                            if not visited[j] and dist_matrix[node, j] <= eps:
                                visited[j] = True
                                queue.append(j)
                    components += 1
            betti_0.append(components)
            
            # Betti-1: approximate as number of edges - vertices + components
            # (Euler characteristic: chi = V - E + F, Betti-1 = E - V + Betti-0 for 1-complex)
            edges_count = 0
            for i in range(n):
                for j in range(i + 1, n):
                    if dist_matrix[i, j] <= eps:
                        edges_count += 1
            # Betti-1 = max(0, edges - vertices + components)
            b1 = max(0, edges_count - n + components)
            betti_1.append(b1)
        
        return betti_0, betti_1
    
    def _persistence_entropy(self, features: List[PersistenceFeature]) -> float:
        """Compute persistence entropy (information-theoretic measure)."""
        if not features:
            return 0.0
        
        persistences = [f.persistence for f in features if f.persistence > 0]
        if not persistences:
            return 0.0
        
        total = sum(persistences)
        if total <= 0:
            return 0.0
        
        probs = [p / total for p in persistences]
        entropy = -sum(p * np.log2(p) for p in probs if p > 0)
        
        return float(entropy)
    
    def _compute_anomaly_score(self, current_entropy: float) -> float:
        """
        Compute anomaly score based on deviation from baseline entropy.
        
        Returns z-score of current entropy vs baseline.
        If no baseline, uses the historical mean.
        """
        if self._baseline_entropy is not None:
            # Compare to baseline
            diff = abs(current_entropy - self._baseline_entropy)
            return float(diff / (self._baseline_entropy + 0.1))  # normalised deviation
        else:
            # No baseline - use heuristic
            # Normal entropy range for financial data: 1-5 bits
            if current_entropy < 0.5:
                return 0.0  # very low entropy = simple structure = normal
            elif current_entropy > 5.0:
                return float((current_entropy - 5.0) / 5.0)  # high entropy = complex = anomalous
            return 0.0
    
    def _regime_change_probability(
        self,
        anomaly_score: float,
        betti_0: List[int],
        betti_1: List[int],
    ) -> float:
        """
        Estimate probability of regime change based on TDA features.
        
        Indicators:
        1. High anomaly score → topology has changed
        2. Betti number spikes → new structure forming
        3. Betti-0 decreasing rapidly → components merging → convergence
        """
        prob = 0.0
        
        # Anomaly contribution
        if anomaly_score > self.anomaly_threshold:
            prob += min(anomaly_score / self.anomaly_threshold * 0.4, 0.4)
        
        # Betti-0 change rate (are components merging quickly?)
        if len(betti_0) >= 4:
            recent_change = abs(betti_0[-1] - betti_0[-4]) / max(betti_0[-4], 1)
            prob += min(recent_change * 0.3, 0.3)
        
        # Betti-1 spike (new loops forming = potential regime transition)
        if len(betti_1) >= 2:
            if betti_1[-1] > betti_1[-2] and betti_1[-1] > 0:
                prob += 0.2
        
        return float(np.clip(prob, 0.0, 1.0))
    
    def _empty_result(self, symbol: str, n_bars: int) -> TDAFeatureSet:
        """Return an empty TDA result for insufficient data."""
        return TDAFeatureSet(
            symbol=symbol,
            n_bars=n_bars,
            features_0d=[],
            features_1d=[],
            betti_0=[1],
            betti_1=[0],
            n_components=0,
            n_loops=0,
            max_persistence_0d=0.0,
            mean_persistence_0d=0.0,
            max_persistence_1d=0.0,
            mean_persistence_1d=0.0,
            persistence_entropy_0d=0.0,
            persistence_entropy_1d=0.0,
            total_persistence_entropy=0.0,
            anomaly_score=0.0,
            regime_change_probability=0.0,
            feature_vector=[0.0] * 10,
            feature_labels=[
                "n_components", "n_loops", "max_persistence_0d", "mean_persistence_0d",
                "max_persistence_1d", "mean_persistence_1d", "entropy_0d", "entropy_1d",
                "anomaly_score", "regime_change_probability",
            ],
        )
