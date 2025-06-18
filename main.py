from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import asyncio
import base64
import os
from typing import Dict, List, Optional, Set
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Companies House Ownership API", version="1.0.0")

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class CompanyRequest(BaseModel):
    company_name: str

class PSCNode(BaseModel):
    id: str
    name: str
    type: str  # 'individual', 'uk_company', 'non_uk_company'
    company_number: Optional[str] = None
    country_of_residence: Optional[str] = None
    nature_of_control: List[str] = []
    children: List['PSCNode'] = []
    is_active: bool = True
    error: Optional[str] = None

class OwnershipResponse(BaseModel):
    root_company: PSCNode
    total_nodes: int
    processing_time: float
    errors: List[str] = []

# Global variables for API configuration
COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY")
BASE_URL = "https://api.company-information.service.gov.uk"

if not COMPANIES_HOUSE_API_KEY:
    logger.warning("COMPANIES_HOUSE_API_KEY not found in environment variables")

def get_auth_headers():
    """Get authentication headers for Companies House API"""
    if not COMPANIES_HOUSE_API_KEY:
        raise HTTPException(status_code=500, detail="API key not configured")
    
    # Companies House API uses basic auth with API key as username and empty password
    credentials = base64.b64encode(f"{COMPANIES_HOUSE_API_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}

async def search_company_by_name(company_name: str) -> Optional[Dict]:
    """Search for a company by name and return the first active match"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/search/companies",
                params={"q": company_name, "items_per_page": 10},
                headers=get_auth_headers(),
                timeout=30.0
            )
            
            if response.status_code == 200:
                data = response.json()
                companies = data.get("items", [])
                
                # Find the first active company that matches
                for company in companies:
                    if (company.get("company_status") == "active" and 
                        company_name.lower() in company.get("title", "").lower()):
                        return company
                
                # If no exact match, return the first active company
                for company in companies:
                    if company.get("company_status") == "active":
                        return company
                        
            return None
    except Exception as e:
        logger.error(f"Error searching for company {company_name}: {str(e)}")
        return None

async def get_company_pscs(company_number: str) -> List[Dict]:
    """Get PSCs (Persons with Significant Control) for a company"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}/company/{company_number}/persons-with-significant-control",
                headers=get_auth_headers(),
                timeout=30.0
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("items", [])
            elif response.status_code == 404:
                logger.info(f"No PSC data found for company {company_number}")
                return []
            else:
                logger.error(f"Error fetching PSCs for {company_number}: {response.status_code}")
                return []
    except Exception as e:
        logger.error(f"Error fetching PSCs for company {company_number}: {str(e)}")
        return []

def determine_entity_type(psc: Dict) -> str:
    """Determine if PSC is an individual, UK company, or non-UK company"""
    kind = psc.get("kind", "")
    
    if "individual" in kind:
        return "individual"
    elif "corporate-entity" in kind or "legal-person" in kind:
        # Check if it's a UK company
        country = psc.get("country_of_residence") or psc.get("identification", {}).get("country_registered")
        if country and country.lower() in ["england", "wales", "scotland", "northern ireland", "united kingdom", "uk"]:
            return "uk_company"
        else:
            return "non_uk_company"
    else:
        return "individual"  # Default fallback

async def build_ownership_tree(company_number: str, company_name: str, visited: Set[str], errors: List[str], depth: int = 0) -> PSCNode:
    """Recursively build ownership tree for a company"""
    
    # Prevent infinite recursion (though we allow unlimited depth)
    if company_number in visited:
        return PSCNode(
            id=company_number,
            name=f"{company_name} (circular reference)",
            type="uk_company",
            company_number=company_number,
            error="Circular reference detected"
        )
    
    visited.add(company_number)
    
    # Create root node
    root_node = PSCNode(
        id=company_number,
        name=company_name,
        type="uk_company",
        company_number=company_number
    )
    
    try:
        # Get PSCs for this company
        pscs = await get_company_pscs(company_number)
        
        for psc in pscs:
            # Skip inactive PSCs
            if not psc.get("ceased_on") is None:
                continue
                
            psc_name = psc.get("name") or psc.get("name_elements", {}).get("forename", "") + " " + psc.get("name_elements", {}).get("surname", "")
            psc_type = determine_entity_type(psc)
            
            nature_of_control = psc.get("natures_of_control", [])
            
            psc_node = PSCNode(
                id=psc.get("links", {}).get("self", f"psc_{len(root_node.children)}"),
                name=psc_name.strip(),
                type=psc_type,
                country_of_residence=psc.get("country_of_residence"),
                nature_of_control=nature_of_control
            )
            
            # If this PSC is a UK company, recursively get its PSCs
            if psc_type == "uk_company":
                identification = psc.get("identification", {})
                psc_company_number = identification.get("registration_number")
                
                if psc_company_number:
                    psc_node.company_number = psc_company_number
                    # Recursive call to get PSCs of this company
                    try:
                        child_tree = await build_ownership_tree(
                            psc_company_number, 
                            psc_name.strip(), 
                            visited.copy(),  # Pass a copy to avoid affecting parallel branches
                            errors,
                            depth + 1
                        )
                        psc_node.children = child_tree.children
                    except Exception as e:
                        error_msg = f"Error processing company {psc_company_number}: {str(e)}"
                        errors.append(error_msg)
                        psc_node.error = error_msg
            
            root_node.children.append(psc_node)
            
    except Exception as e:
        error_msg = f"Error processing PSCs for {company_number}: {str(e)}"
        errors.append(error_msg)
        root_node.error = error_msg
    
    return root_node

def count_nodes(node: PSCNode) -> int:
    """Count total nodes in the tree"""
    count = 1
    for child in node.children:
        count += count_nodes(child)
    return count

@app.get("/")
async def root():
    return {"message": "Companies House Ownership API", "status": "running"}

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "api_key_configured": bool(COMPANIES_HOUSE_API_KEY),
        "timestamp": datetime.now().isoformat()
    }

@app.post("/ownership-structure", response_model=OwnershipResponse)
async def get_ownership_structure(request: CompanyRequest):
    """Get the full ownership structure for a company"""
    start_time = datetime.now()
    errors = []
    
    try:
        # First, search for the company
        company_info = await search_company_by_name(request.company_name)
        
        if not company_info:
            raise HTTPException(status_code=404, detail=f"Company '{request.company_name}' not found")
        
        company_number = company_info.get("company_number")
        company_name = company_info.get("title")
        
        if not company_number:
            raise HTTPException(status_code=400, detail="Could not determine company number")
        
        # Build the ownership tree
        ownership_tree = await build_ownership_tree(
            company_number, 
            company_name, 
            set(), 
            errors
        )
        
        total_nodes = count_nodes(ownership_tree)
        processing_time = (datetime.now() - start_time).total_seconds()
        
        return OwnershipResponse(
            root_company=ownership_tree,
            total_nodes=total_nodes,
            processing_time=processing_time,
            errors=errors
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
