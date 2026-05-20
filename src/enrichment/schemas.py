"""Pydantic schemas for enrichment module outputs."""
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class LinkedInProfile(BaseModel):
    full_name: Optional[str] = None
    headline: Optional[str] = None
    about: Optional[str] = None
    location: Optional[str] = None
    current_company: Optional[str] = None
    current_title: Optional[str] = None
    raw: dict = Field(default_factory=dict)


class LinkedInPost(BaseModel):
    text: Optional[str] = None
    posted_at: Optional[str] = None
    url: Optional[str] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    raw: dict = Field(default_factory=dict)

    # Some Apify actors (e.g. supreme_coder/linkedin-post) return `comments`
    # and reaction fields as lists of objects rather than counts. Downstream
    # consumers (scoring prompts, UI badges) want a number, so collapse
    # list-shaped inputs to len() at validation time.
    @field_validator("comments", "likes", mode="before")
    @classmethod
    def _coerce_list_to_count(cls, v: Any) -> Any:
        if isinstance(v, list):
            return len(v)
        return v


class CompanyDetails(BaseModel):
    name: Optional[str] = None
    tagline: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    headquarters: Optional[str] = None
    founded: Optional[int] = None
    website: Optional[str] = None
    raw: dict = Field(default_factory=dict)


class CompanyPost(BaseModel):
    text: Optional[str] = None
    posted_at: Optional[str] = None
    url: Optional[str] = None
    raw: dict = Field(default_factory=dict)


class NewsItem(BaseModel):
    title: str
    url: str
    snippet: Optional[str] = None
    published_at: Optional[str] = None
    score: Optional[float] = None
