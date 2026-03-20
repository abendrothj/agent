"""
Risk Tier Classifier - Analyzes requests to determine risk level (Tier 1-4)
"""
import logging
from typing import Tuple
from enum import IntEnum

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    """Risk tier classification (immutable per base_policy.yaml)"""
    TIER_1_SAFE = 1
    TIER_2_MINOR = 2
    TIER_3_MAJOR = 3
    TIER_4_CRITICAL = 4


class RiskClassifier:
    """Analyzes prompt/scope to assign risk tier"""
    
    # Keywords indicating different risk levels
    CRITICAL_KEYWORDS = [
        "vault", "memory", "ledger", "approval_rules", "rejection_cache",
        "override", "rollback", "policy", "system", "critical",
        "permission", "access", "credential", "secret", "mfa",
    ]
    
    MAJOR_KEYWORDS = [
        "code", "github", "pr", "commit", "modify", "deploy",
        "config", "change", "update", "package", "version",
    ]
    
    MINOR_KEYWORDS = [
        "test", "local", "temp", "sandbox", "query",
        "read", "list", "fetch", "info",
    ]
    
    # Actions that inherently require approval
    APPROVAL_REQUIRED_ACTIONS = [
        "execute_code",
        "modify_config",
        "create_pr",
        "deploy",
        "change_policy",
        "override_approval",
    ]
    
    def classify(
        self,
        prompt: str,
        system_context: str = "",
        scope: str = "local",
    ) -> Tier:
        """Classify request into risk tier"""
        
        combined_text = (prompt + " " + system_context).lower()
        scope_lower = scope.lower()
        
        # Check for critical indicators
        critical_score = self._score_keywords(combined_text, self.CRITICAL_KEYWORDS)
        if critical_score >= 2:
            logger.info(f"Classified as TIER_4_CRITICAL (critical_score={critical_score})")
            return Tier.TIER_4_CRITICAL
        
        # Check for major indicators
        major_score = self._score_keywords(combined_text, self.MAJOR_KEYWORDS)
        if major_score >= 2 or "github" in combined_text or "pr" in combined_text:
            logger.info(f"Classified as TIER_3_MAJOR (major_score={major_score})")
            return Tier.TIER_3_MAJOR
        
        # Check for scope
        if scope_lower == "system" or scope_lower == "global":
            logger.info(f"Classified as TIER_3_MAJOR (scope={scope})")
            return Tier.TIER_3_MAJOR
        
        # Check for minor indicators
        minor_score = self._score_keywords(combined_text, self.MINOR_KEYWORDS)
        if minor_score >= 2:
            logger.info(f"Classified as TIER_2_MINOR (minor_score={minor_score})")
            return Tier.TIER_2_MINOR
        
        # Check if action requires approval
        for action in self.APPROVAL_REQUIRED_ACTIONS:
            if action in combined_text:
                logger.info(f"Classified as TIER_2_MINOR (action={action})")
                return Tier.TIER_2_MINOR
        
        # Default to safe
        logger.info("Classified as TIER_1_SAFE (default)")
        return Tier.TIER_1_SAFE
    
    def _score_keywords(self, text: str, keywords: list) -> int:
        """Score text against keyword list"""
        score = 0
        for keyword in keywords:
            if keyword in text:
                score += 1
        return score
    
    def requires_mfa(self, tier: Tier) -> bool:
        """Check if tier requires MFA"""
        return tier in [Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def requires_approval(self, tier: Tier) -> bool:
        """Check if tier requires approval"""
        return tier in [Tier.TIER_2_MINOR, Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def requires_shadow_baseline(self, tier: Tier) -> bool:
        """Check if tier requires Shadow baseline before canary"""
        return tier in [Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def get_shadow_min_hours(self, tier: Tier) -> int:
        """Get minimum Shadow baseline hours for tier"""
        if tier == Tier.TIER_4_CRITICAL:
            return 48
        elif tier == Tier.TIER_3_MAJOR:
            return 24
        return 0
    
    def get_similarity_threshold(self, tier: Tier) -> float:
        """Get semantic similarity threshold for tier"""
        if tier == Tier.TIER_4_CRITICAL:
            return 0.90
        elif tier == Tier.TIER_3_MAJOR:
            return 0.85
        return 0.70
    
    def get_rate_limit(self, tier: Tier) -> str:
        """Get rate limit for tier (requests/hour)"""
        limits = {
            Tier.TIER_1_SAFE: "1000/hour",
            Tier.TIER_2_MINOR: "100/hour",
            Tier.TIER_3_MAJOR: "10/hour",
            Tier.TIER_4_CRITICAL: "1/hour",
        }
        return limits.get(tier, "10/hour")
    
    def get_timeout_seconds(self, tier: Tier) -> int:
        """Get execution timeout for tier"""
        timeouts = {
            Tier.TIER_1_SAFE: 30,
            Tier.TIER_2_MINOR: 60,
            Tier.TIER_3_MAJOR: 300,
            Tier.TIER_4_CRITICAL: 600,
        }
        return timeouts.get(tier, 60)
    
    def is_read_only(self, tier: Tier) -> bool:
        """Check if tier is read-only"""
        return tier == Tier.TIER_1_SAFE
