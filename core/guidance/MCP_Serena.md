# Serena MCP Server

**Purpose**: Semantic code understanding with project memory and session persistence

## Triggers
- Symbol operations: rename, extract, move functions/classes
- Project-wide code navigation and exploration
- Multi-language projects requiring LSP integration
- Project activation and memory-driven development workflows
- Large codebase analysis (>50 files, complex architecture)

## Choose When
- **For semantic understanding**: Symbol references, dependency tracking, LSP integration
- **For session persistence**: Project context, memory management, cross-session learning
- **For large projects**: Multi-language codebases requiring architectural understanding
- **Not for simple edits**: Basic text replacements, style enforcement, bulk operations

## Works Best With
- **Sequential**: Serena provides project context → Sequential performs architectural analysis

## Examples
```
"rename getUserData function everywhere" → Serena (symbol operation with dependency tracking)
"find all references to this class" → Serena (semantic search and navigation)
"analyze project architecture" → Serena (project-wide semantic analysis)
"find all usages of this type" → Serena (cross-file reference tracking)
```