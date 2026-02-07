from typing import TypedDict, List, Dict
import os
import requests
from types import SimpleNamespace
import json
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


load_dotenv()


class OllamaClient:
    def __init__(self, host: str = "http://localhost:11434", model: str = "llama2:latest", temperature: float = 0.3):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature

    # Compatibility with LangChain-style clients
    def invoke(self, prompt: str, max_tokens: int = 512) -> SimpleNamespace:
        return self.generate(prompt, max_tokens=max_tokens)

    def generate(self, prompt: str, max_tokens: int = 512) -> SimpleNamespace:
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "temperature": self.temperature,
            "max_tokens": max_tokens
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()

        # Ollama may stream multiple JSON objects (NDJSON) or return a single JSON blob.
        # Try normal JSON parse first, otherwise fall back to taking the last JSON object
        # from the response body. If that still fails, return the raw text.
        try:
            data = resp.json()
        except Exception:
            body = resp.text or ""
            parsed_any = False
            text_accum = ""
            # Attempt to parse every JSON line (NDJSON/streaming) and accumulate textual pieces
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                if not (line.startswith("{") or line.startswith("[")):
                    continue
                try:
                    part = json.loads(line)
                except Exception:
                    continue
                parsed_any = True
                # collect common fields
                if isinstance(part, dict):
                    if "response" in part and isinstance(part["response"], str):
                        text_accum += part["response"]
                    elif "text" in part and isinstance(part["text"], str):
                        text_accum += part["text"]
                    elif "choices" in part:
                        try:
                            for ch in part.get("choices", []):
                                for item in ch.get("content", []):
                                    if isinstance(item, dict) and item.get("type") == "response.text":
                                        text_accum += item.get("text", "")
                                    elif isinstance(item, dict) and "text" in item:
                                        text_accum += item.get("text", "")
                        except Exception:
                            pass

            if parsed_any and text_accum:
                return SimpleNamespace(content=text_accum)

            # Fallback: try to parse last JSON object in the stream
            data = None
            for line in reversed(body.splitlines()):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{") or line.startswith("["):
                    try:
                        data = json.loads(line)
                        break
                    except Exception:
                        continue
            if data is None:
                return SimpleNamespace(content=body)

        # Extract text from common Ollama response shapes
        text = ""
        if isinstance(data, dict):
            if "response" in data and isinstance(data["response"], str):
                text = data["response"]
            elif "text" in data and isinstance(data["text"], str):
                text = data["text"]
            elif "choices" in data:
                try:
                    for ch in data.get("choices", []):
                        # content is often a list of {'type': 'response.text', 'text': '...'}
                        for item in ch.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "response.text":
                                text += item.get("text", "")
                            elif isinstance(item, dict) and "text" in item:
                                text += item.get("text", "")
                except Exception:
                    text = str(data)
            else:
                text = str(data)
        else:
            text = str(data)

        return SimpleNamespace(content=text)

load_dotenv()

class CodeReviewRequest(BaseModel):
    code: str

class CodeReviewState(TypedDict):
    """State that goes through nodes of our graph"""
    code: str
    initial_analysis: str
    issues: List[str]
    final_report: str

class SimpleCodeReviewAgent:
    def __init__(self):
        # Use local Ollama + llama2:latest
        self.llm = OllamaClient(
            host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            model=os.getenv("OLLAMA_MODEL", "llama2:latest"),
            temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.3"))
        )

        self.graph = self._build_graph()

    def _normalize_issues(self, text: str) -> List[str]:
        if not text:
            return ["- no issues found"]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(line.lower() == "no issues found" for line in lines):
            return ["- no issues found"]
        issues = [line for line in lines if line.startswith("-")]
        if issues:
            return issues
        # Fallback: convert any non-empty lines into bullet issues
        return [f"- {line}" for line in lines]

    def _analysis_agent(self, state: CodeReviewState) -> Dict:
        """Step1: Analyse the code"""
        prompt = f"""Analyse the code briefly:
            {state['code']}
        Focus on: purpose, structure and concerns.  
"""
        response = self.llm.invoke(prompt)
        return {"initial_analysis": response.content}
    
    def _find_issues(self, state: CodeReviewState) -> Dict:
        """Step2 : Find the issues in code"""
        prompt = f"""
    List 3-5 concrete code issues in the code below.
    Output ONLY bullet points starting with "-".
    No explanations.

    Code:
    {state['code']}
    """
        
        response = self.llm.invoke(prompt)
        issues = self._normalize_issues(response.content)

        if issues == ["- no issues found"]:
            retry_prompt = f"""
    List concrete issues as bullet points starting with "-".
    If there are no issues, return exactly: No issues found

Code:
{state['code']}
"""
            retry_response = self.llm.invoke(retry_prompt)
            issues = self._normalize_issues(retry_response.content)

        return {"issues": issues}
    
    def _generate_report(self, state: CodeReviewState) -> Dict:
        """Step3: Generate report from the review"""

        issues_text = '\n'.join(state['issues'])

        prompt = f"""Create a code review report:
        
        Analysis: {state['initial_analysis']}
        Issues: {state['issues']}

        Format Summary, Issues, and Recommendation.
"""
        
        response = self.llm.invoke(prompt)

        return {"final_report": response.content}
    
    def _build_graph(self) -> StateGraph:
        """Build the langgraph workflow"""

        workflow = StateGraph(CodeReviewState)

        #Add nodes 
        workflow.add_node("analyzer", self._analysis_agent)
        workflow.add_node("issue_finder", self._find_issues)
        workflow.add_node("report_generator", self._generate_report)

        # Add edges 
        workflow.set_entry_point("analyzer")
        workflow.add_edge("analyzer", "issue_finder")
        workflow.add_edge("issue_finder", "report_generator")
        workflow.add_edge("report_generator", END)

        return workflow.compile()
    
agent = SimpleCodeReviewAgent()
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins =["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/review")
def review_code(request: CodeReviewRequest):

    initial_state = {
        "code": request.code,
        "initial_analysis": "",
        "issues": [],
        "final_report": ""
    }

    try:
        result = agent.graph.invoke(initial_state)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "analysis": result['initial_analysis'],
        "issues": result["issues"],
        "report": result["final_report"]
    }