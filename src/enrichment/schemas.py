"""Pydantic schemas for enrichment module outputs."""
from typing import Optional

from pydantic import BaseModel, Field


class LinkedInProfile(BaseModel):
    full_name: Optional[str] = None
    headline: Optional[str] = None
    about: Optional[str] = None
    location: Optional[str] = None
    current_company: Optional[str] = None
    current_title: Optional[str] = None
    raw: dict = Field(default_factory=dict)


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


class NewsItem(BaseModel):
    title: str
    url: str
    snippet: Optional[str] = None
    published_at: Optional[str] = None
    score: Optional[float] = None
