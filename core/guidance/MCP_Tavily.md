# Tavily MCP Server

**Purpose**: Web search and real-time information retrieval for research and current events

## Triggers
- Web search requirements beyond Claude's knowledge cutoff
- Current events, news, and real-time information needs
- Market research and competitive analysis tasks
- Technical documentation not in training data
- Academic research requiring recent publications
- Fact-checking and verification needs
- Deep research investigations requiring multi-source analysis
- Research tasks requiring multiple web sources

## Choose When
- **Over WebSearch**: When you need structured search with advanced filtering
- **Over WebFetch**: When you need multi-source search, not single page extraction
- **For research**: Comprehensive investigations requiring multiple sources
- **For current info**: Events, updates, or changes after knowledge cutoff
- **Not for**: Simple questions answerable from training, code generation, local file operations

## Works Best With
- **Sequential**: Tavily provides raw information → Sequential analyzes and synthesizes
- **Playwright**: Tavily discovers URLs → Playwright extracts complex content
- **Context7**: Tavily searches for updates → Context7 provides stable documentation
- **Serena**: Tavily performs searches → Serena stores research sessions

## Configuration
Requires TAVILY_API_KEY environment variable from https://app.tavily.com

## Search Capabilities
- **Web Search**: General web searches with ranking algorithms
- **News Search**: Time-filtered news and current events
- **Academic Search**: Scholarly articles and research papers
- **Domain Filtering**: Include/exclude specific domains
- **Content Extraction**: Full-text extraction from search results
- **Freshness Control**: Prioritize recent content
- **Multi-Round Searching**: Iterative refinement based on gaps

## Examples
```
"latest TypeScript features 2024" → Tavily (current technical information)
"OpenAI GPT updates this week" → Tavily (recent news and updates)
"quantum computing breakthroughs 2024" → Tavily (recent research)
"best practices React Server Components" → Tavily (current best practices)
"explain recursion" → Native Claude (general concept explanation)
"write a Python function" → Native Claude (code generation)
```

## Fallback
If Tavily fails or rate-limits, use native `WebSearch` tool as alternative.