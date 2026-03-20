"""
Safety Enforcement - Policy validation and access control
"""
import logging
from typing import Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class SafetyValidator:
    """Validates requests against safety policies"""
    
    BLOCKED_PATTERNS = [
        "delete database",
        "drop table",
        "truncate",
        "rm -rf /",
        "format /",
        "::1",  # IPv6 loopback
    ]
    
    RATE_LIMITS = {
        1: ("1000/hour", 1000),
        2: ("100/hour", 100),
        3: ("10/hour", 10),
        4: ("1/hour", 1),
    }
    
    def __init__(self):
        self.violation_count = 0
    
    def validate_prompt(self, prompt: str, tier: int) -> Tuple[bool, str]:
        """
        Validate prompt against safety policies
        
        Returns: (valid, reason)
        """
        
        prompt_lower = prompt.lower()
        
        # Check for blocked patterns
        for pattern in self.BLOCKED_PATTERNS:
            if pattern.lower() in prompt_lower:
                logger.warning(f"Blocked pattern detected: {pattern}")
                self.violation_count += 1
                return False, f"Request matches blocked pattern: '{pattern}'"
        
        # Check prompt length (XSS prevention)
        if len(prompt) > 100000:
            return False, "Prompt too long (>100KB)"
        
        # Check for repeated failures (DoS prevention)
        if self.violation_count > 100:
            return False, "Too many violations detected; service temporarily restricted"
        
        return True, "Prompt valid"
    
    def validate_scope(self, scope: str, tier: int) -> Tuple[bool, str]:
        """
        Validate requested scope against tier
        
        Returns: (valid, reason)
        """
        
        tier_allowed_scopes = {
            1: ["read", "query"],
            2: ["local", "test", "sandbox"],
            3: ["local", "github", "config"],
            4: ["*"],  # All scopes
        }
        
        allowed = tier_allowed_scopes.get(tier, [])
        
        if scope not in allowed and "*" not in allowed:
            return False, f"Scope '{scope}' not allowed for Tier {tier}"
        
        return True, "Scope valid"
    
    def should_audit_log(self, tier: int) -> bool:
        """Check if request should be fully audit-logged"""
        return tier >= 2
    
    def should_require_mfa(self, tier: int) -> bool:
        """Check if MFA signature is required"""
        return tier >= 3


class RateLimiter:
    """Token bucket rate limiter"""
    
    def __init__(self, tier: int):
        self.tier = tier
        self.tokens = 0
        self.last_refill = datetime.utcnow()
    
    def is_allowed(self) -> bool:
        """Check if request is allowed under rate limit"""
        
        # Refill tokens based on tier (per hour)
        tier_limits = {
            1: 1000,
            2: 100,
            3: 10,
            4: 1,
        }
        
        limit = tier_limits.get(self.tier, 10)
        
        # Simple check (in production: use proper token bucket)
        current_count = getattr(self, "_request_count", 0)
        
        if current_count >= limit:
            return False
        
        self._request_count = current_count + 1
        return True
    
    def reset_hourly(self):
        """Reset counter every hour"""
        self._request_count = 0
