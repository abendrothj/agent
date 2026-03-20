"""
Metrics Evaluator - Canary & Shadow promotion logic
"""
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


class MetricsEvaluator:
    """Evaluates canary/shadow promotion readiness based on metrics"""
    
    def __init__(
        self,
        tier_3_success_threshold: float = 0.95,
        tier_4_success_threshold: float = 0.99,
        tier_3_error_threshold: float = 0.05,
        tier_4_error_threshold: float = 0.01,
    ):
        self.tier_3_success_threshold = tier_3_success_threshold
        self.tier_4_success_threshold = tier_4_success_threshold
        self.tier_3_error_threshold = tier_3_error_threshold
        self.tier_4_error_threshold = tier_4_error_threshold
    
    def evaluate_shadow_readiness(
        self,
        tier: int,
        duration_hours: float,
        success_rate: float,
        error_rate: float,
        sample_count: int,
    ) -> Tuple[bool, str]:
        """
        Evaluate if Shadow baseline meets promotion requirements
        
        Returns: (eligible, reason)
        """
        
        # Check minimum duration
        min_hours = 48 if tier == 4 else 24 if tier == 3 else 1
        if duration_hours < min_hours:
            return False, f"Baseline age {duration_hours:.1f}h < {min_hours}h minimum"
        
        # Check minimum sample size
        min_samples = 50 if tier == 4 else 20 if tier == 3 else 5
        if sample_count < min_samples:
            return False, f"Sample count {sample_count} < {min_samples} minimum"
        
        # Check success rate
        success_threshold = self.tier_4_success_threshold if tier == 4 else self.tier_3_success_threshold
        if success_rate < success_threshold:
            return False, f"Success rate {success_rate:.2%} < {success_threshold:.2%}"
        
        # Check error rate
        error_threshold = self.tier_4_error_threshold if tier == 4 else self.tier_3_error_threshold
        if error_rate > error_threshold:
            return False, f"Error rate {error_rate:.2%} > {error_threshold:.2%}"
        
        logger.info(f"Shadow baseline meets Tier {tier} requirements")
        return True, "Shadow baseline ready for promotion"
    
    def evaluate_canary_eligibility(
        self,
        tier: int,
        semantic_similarity: float,
        baseline_age_hours: float,
    ) -> Tuple[bool, str]:
        """
        Evaluate if request eligible for canary promotion
        
        Returns: (eligible, reason)
        """
        
        # Check semantic similarity
        similarity_threshold = 0.90 if tier == 4 else 0.85 if tier == 3 else 0.80
        if semantic_similarity < similarity_threshold:
            return False, f"Semantic similarity {semantic_similarity:.2%} < {similarity_threshold:.2%}"
        
        # Check baseline age (must meet minimum for tier)
        min_baseline_age = 48 if tier == 4 else 24 if tier == 3 else 1
        if baseline_age_hours < min_baseline_age:
            return False, f"Baseline age {baseline_age_hours:.1f}h < {min_baseline_age}h minimum"
        
        logger.info(f"Request eligible for Tier {tier} canary")
        return True, "Eligible for canary promotion"
    
    def evaluate_canary_failure(
        self,
        tier: int,
        success_rate: float,
        error_rate: float,
    ) -> Tuple[bool, str]:
        """
        Evaluate if canary has failed and should rollback
        
        Returns: (should_rollback, reason)
        """
        
        # Check thresholds
        success_threshold = self.tier_4_success_threshold if tier == 4 else self.tier_3_success_threshold
        error_threshold = self.tier_4_error_threshold if tier == 4 else self.tier_3_error_threshold
        
        if success_rate < success_threshold:
            return True, f"Canary success rate {success_rate:.2%} below threshold"
        
        if error_rate > error_threshold:
            return True, f"Canary error rate {error_rate:.2%} exceeded threshold"
        
        return False, "Canary metrics healthy"
    
    def compute_canary_traffic_percent(
        self,
        tier: int,
        elapsed_minutes: int,
    ) -> int:
        """
        Compute canary traffic allocation based on elapsed time
        
        Tier 3: ramp from 0% to 100% over ~60 minutes
        Tier 4: ramp from 0% to 100% over ~2880 minutes (48h)
        """
        
        if tier == 4:
            # Tier 4: conservative ramp
            schedule = [
                (0, 0),
                (10, 1),
                (120, 5),
                (360, 10),
                (1440, 50),
                (2880, 100),
            ]
        else:
            # Tier 3: faster ramp
            schedule = [
                (0, 0),
                (5, 10),
                (15, 25),
                (30, 50),
                (60, 100),
            ]
        
        # Find appropriate amount for elapsed time
        for i, (time_min, percent) in enumerate(schedule):
            if elapsed_minutes < time_min:
                if i == 0:
                    return 0
                # Interpolate between previous and current
                prev_time, prev_percent = schedule[i - 1]
                time_diff = time_min - prev_time
                percent_diff = percent - prev_percent
                elapsed_diff = elapsed_minutes - prev_time
                interpolated = prev_percent + (percent_diff * elapsed_diff / time_diff)
                return int(interpolated)
        
        return 100
    
    def should_use_rejection_cache(self, tier: int) -> bool:
        """Check if Tier 4 24h rejection cache should apply"""
        return tier == 4
    
    def compute_semantic_drift(
        self,
        baseline_embeddings: list,
        recent_embeddings: list,
    ) -> float:
        """
        Compute semantic drift between baseline and recent outputs
        
        Returns: euclidean distance (0.0 = identical, 1.0+ = very different)
        """
        if not baseline_embeddings or not recent_embeddings:
            return 0.0
        
        # Simple centroid-based drift
        baseline_mean = self._compute_centroid(baseline_embeddings)
        recent_mean = self._compute_centroid(recent_embeddings)
        
        drift = self._euclidean_distance(baseline_mean, recent_mean)
        return drift
    
    def _compute_centroid(self, vectors: list) -> list:
        """Compute centroid of vector list"""
        if not vectors:
            return []
        
        dim = len(vectors[0])
        centroid = [0.0] * dim
        
        for vec in vectors:
            for i, val in enumerate(vec):
                centroid[i] += val
        
        centroid = [x / len(vectors) for x in centroid]
        return centroid
    
    def _euclidean_distance(self, vec1: list, vec2: list) -> float:
        """Compute euclidean distance between vectors"""
        if len(vec1) != len(vec2):
            return float('inf')
        
        sum_sq = sum((x - y) ** 2 for x, y in zip(vec1, vec2))
        return sum_sq ** 0.5
