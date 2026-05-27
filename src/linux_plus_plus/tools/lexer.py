import re
from prompt_toolkit.shortcuts import prompt
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.document import Document
from typing import Callable, List, Tuple

class LinuxppRegexLexer(Lexer):
    def __init__(self, regex_mapping: dict):
        super().__init__()
        self.regex_mapping = regex_mapping

    def lex_document(self, document: Document) -> Callable[[int], List[Tuple[str, str]]]:
        def lex(_lineno: int) -> List[Tuple[str, str]]:
            line = document.text
            tokens: List[Tuple[str, str]] = []
            while line:
                found = False
                for pattern, style in self.regex_mapping.items():
                    match = pattern.match(line)
                    if match:
                        tokens.append((style, match.group(0)))
                        line = line[match.end():]
                        found = True
                        break
                if not found:
                    # Default style for unmatched characters
                    tokens.append(('', line[0]))
                    line = line[1:]
            return tokens
        return lex
    
regex_mapping = {
    # 1. Whitespace
    re.compile(r'^\s+'): '',

    # 2. Comments (Block comments first, then single-line)
    re.compile(r'^<#[\s\S]*?#>'): '#6a9955',  # Block comments
    re.compile(r'^#.*'): '#6a9955',           # Single line comments

    # 3. Strings (Double and Single quotes)
    # Note: This is a basic implementation. PowerShell string escaping is complex.
    re.compile(r'^"[^"]*"'): '#ce9178',       # Double-quoted strings
    re.compile(r"^'[^']*'"): '#ce9178',       # Single-quoted strings

    # 4. Variables ($var, ${var with spaces})
    re.compile(r'^\$[a-zA-Z_]\w*'): '#9cdcfe', 
    re.compile(r'^\$\{[^}]+\}'): '#9cdcfe',

    # 5. Cmdlets/Functions (Verb-Noun format like Get-Process)
    re.compile(r'^[a-zA-Z_]\w*-[a-zA-Z_]\w*'): '#dcdcaa',

    # 6. Reserved Keywords
    re.compile(r'^\b(if|else|elseif|switch|while|for|foreach|do|until|break|continue|return|function|filter|class|try|catch|finally|throw|param|begin|process|end)\b', re.IGNORECASE): '#c586c0',

    # 7. PowerShell Operators and Parameters
    # Specific comparison/logical operators matched before generic parameters
    re.compile(r'^-(eq|ne|gt|ge|lt|le|and|or|not|match|replace|split|join|in|contains)\b', re.IGNORECASE): '#ffb86c',
    re.compile(r'^-[a-zA-Z_]\w*'): '#d19a66', # Generic Parameters (e.g., -Force, -Name)

    # 8. Numbers (Hex and Decimal/Float)
    re.compile(r'^0x[0-9a-fA-F]+'): '#b5cea8', # Hexadecimals
    re.compile(r'^\d+(\.\d+)?'): '#b5cea8',    # Integers and Floats

    # 9. Standard Math/Assignment Operators and Pipes
    re.compile(r'^[+\-*/=%|!><&]+'): '#ff70e5', 

    # 10. Punctuation/Brackets
    re.compile(r'^[{}[\](),;]'): '#cccccc',

    # 11. Generic words (Aliases, standard executables like 'echo', 'git', etc.)
    re.compile(r'^[a-zA-Z_]\w*'): '#4fc1ff'
}


def main():
    # Define patterns and styles (colors)
    # Example: Highlight numbers in orange, operators in magenta
    regex_mapping = {
        re.compile(r'^\d+(\.\d+)?'): '#ffa500',  # Numbers
        re.compile(r'^[+\-*/()]'): '#ff70e5',    # Operators
        re.compile(r'^\s+'): '',                 # Whitespace
    }

    calculator_lexer = LinuxppRegexLexer(regex_mapping)

    # Use the lexer in the prompt
    result = prompt("Enter Equation: ", lexer=calculator_lexer)
    print('Result:', result)   


if __name__ == "__main__":
    main()
