"""
GitHub Integration Provider - PR creation and approval webhook handling
"""
import logging
from typing import Optional, Dict, Any
import os

logger = logging.getLogger(__name__)


class GithubProvider:
    """GitHub API integration for PR creation and webhook processing"""
    
    def __init__(self, api_token: Optional[str] = None, repo: Optional[str] = None):
        self.api_token = api_token or os.getenv("GITHUB_TOKEN")
        self.repo = repo or os.getenv("GITHUB_REPO", "ja/agent")
        self.base_url = "https://api.github.com"
        
        if not self.api_token:
            logger.warning("GITHUB_TOKEN not set; GitHub integration disabled")
    
    def create_pr(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
    ) -> Optional[Dict[str, Any]]:
        """Create GitHub pull request via REST API"""
        
        if not self.api_token:
            logger.error("GitHub token not configured")
            return None
        
        try:
            import requests
            
            url = f"{self.base_url}/repos/{self.repo}/pulls"
            headers = {
                "Authorization": f"token {self.api_token}",
                "Accept": "application/vnd.github.v3+json",
            }
            payload = {
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
            }
            
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            logger.info(f"Created PR #{data['number']}: {data['html_url']}")
            return {
                "number": data["number"],
                "title": data["title"],
                "url": data["html_url"],
                "status": data["state"],
            }
        
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return None
    
    def request_approval(
        self,
        pr_number: int,
        approval_body: str,
    ) -> bool:
        """Post approval-request comment on GitHub PR"""
        
        if not self.api_token:
            logger.error("GitHub token not configured")
            return False
        
        try:
            import requests
            
            url = f"{self.base_url}/repos/{self.repo}/issues/{pr_number}/comments"
            headers = {
                "Authorization": f"token {self.api_token}",
                "Accept": "application/vnd.github.v3+json",
            }
            resp = requests.post(url, json={"body": approval_body}, headers=headers, timeout=10)
            resp.raise_for_status()
            
            logger.info(f"Posted approval request on PR #{pr_number}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to post approval request: {e}")
            return False
    
    def get_pr_status(self, pr_number: int) -> Optional[str]:
        """Get PR status from GitHub API"""
        
        if not self.api_token:
            return None
        
        try:
            import requests
            
            url = f"{self.base_url}/repos/{self.repo}/pulls/{pr_number}"
            headers = {
                "Authorization": f"token {self.api_token}",
                "Accept": "application/vnd.github.v3+json",
            }
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            state = data.get("state", "unknown")
            merged = data.get("merged", False)
            if merged:
                state = "merged"
            
            logger.info(f"PR #{pr_number} status: {state}")
            return state
        
        except Exception as e:
            logger.error(f"Failed to get PR status: {e}")
            return None


class ApprovalProvider:
    """Centralized approval handling (GitHub, email, manual token)"""
    
    def __init__(self):
        self.github = GithubProvider()
    
    async def request_approval(
        self,
        request_id: str,
        tier: int,
        prompt: str,
        reason: str,
    ) -> bool:
        """
        Request approval through configured channels
        
        Tier 2: Can be self-approved
        Tier 3: Requires human approval + 24h baseline
        Tier 4: Requires MFA + 2 approvers + 48h baseline
        """
        
        if tier == 2:
            logger.info(f"Tier 2 auto-approvable: {request_id}")
            return True
        
        if tier == 3:
            # Create GitHub PR and request approval
            pr_title = f"[Agent] Tier 3 Approval Request: {prompt[:60]}"
            pr_body = f"Request ID: {request_id}\n\nReason: {reason}\n\nPrompt: {prompt}"
            
            pr = self.github.create_pr(
                title=pr_title,
                body=pr_body,
                head_branch=f"agent/tier3-{request_id[:8]}",
            )
            
            if pr:
                logger.info(f"Created approval PR: {pr['url']}")
                return True
        
        if tier == 4:
            # Require MFA + manual approval
            logger.warning(f"Tier 4 requires MFA approval: {request_id}")
            return False
        
        return False
