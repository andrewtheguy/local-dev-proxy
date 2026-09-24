//! TOML syntax highlighting for the configuration editor.
//!
//! A forgiving lexer rather than a parser: the editor highlights text while it
//! is being typed, so unterminated strings, unbalanced brackets and stray
//! characters still produce tokens and never an error.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Plain,
    Comment,
    Table,
    Key,
    String,
    Number,
    Boolean,
    Punctuation,
}

/// A run of text on one line; `column` counts characters from the line start.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Token {
    pub line: usize,
    pub column: usize,
    pub text: String,
    pub kind: Kind,
}

/// Every non-whitespace run of `text`, classified. A token never spans lines.
pub fn tokenize(text: &str) -> Vec<Token> {
    let mut lexer = Lexer::new(text);
    lexer.run();
    lexer.tokens
}

/// The line and character column of a byte offset into `text`.
pub fn position(text: &str, byte_offset: usize) -> (usize, usize) {
    let mut end = byte_offset.min(text.len());
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    let before = &text[..end];
    let line_start = before.rfind('\n').map_or(0, |i| i + 1);
    (
        before.matches('\n').count(),
        before[line_start..].chars().count(),
    )
}

struct Lexer {
    chars: Vec<char>,
    /// Line and column of each char.
    positions: Vec<(usize, usize)>,
    i: usize,
    /// Open `[` arrays and `{` inline tables.
    nesting: Vec<char>,
    /// The next word is a key: at the start of a top-level line, or after
    /// `{` or `,` inside an inline table.
    expect_key: bool,
    tokens: Vec<Token>,
}

impl Lexer {
    fn new(text: &str) -> Self {
        let chars: Vec<char> = text.chars().collect();
        let mut positions = Vec::with_capacity(chars.len());
        let (mut line, mut column) = (0, 0);
        for &c in &chars {
            positions.push((line, column));
            if c == '\n' {
                line += 1;
                column = 0;
            } else {
                column += 1;
            }
        }
        Self {
            chars,
            positions,
            i: 0,
            nesting: Vec::new(),
            expect_key: true,
            tokens: Vec::new(),
        }
    }

    fn peek(&self, offset: usize) -> Option<char> {
        self.chars.get(self.i + offset).copied()
    }

    /// Emit `start..self.i`, one token per line it covers.
    fn emit(&mut self, kind: Kind, start: usize) {
        let mut piece_start = start;
        for i in start..=self.i {
            if i == self.i || self.chars[i] == '\n' {
                // Whitespace draws nothing: a multi-line string's indentation
                // stays out of its tokens.
                let piece = &self.chars[piece_start..i];
                let indent = piece.iter().take_while(|c| c.is_whitespace()).count();
                let text: String = piece[indent..].iter().collect();
                let text = text.trim_end();
                if !text.is_empty() {
                    let (line, column) = self.positions[piece_start + indent];
                    self.tokens.push(Token {
                        line,
                        column,
                        text: text.to_owned(),
                        kind,
                    });
                }
                piece_start = i + 1;
            }
        }
    }

    fn run(&mut self) {
        while let Some(c) = self.peek(0) {
            let start = self.i;
            match c {
                '\n' => {
                    self.i += 1;
                    if self.nesting.is_empty() {
                        self.expect_key = true;
                    }
                }
                c if c.is_whitespace() => self.i += 1,
                '#' => {
                    self.skip_to_line_end();
                    self.emit(Kind::Comment, start);
                }
                '[' if self.expect_key && self.nesting.is_empty() => self.table_header(),
                '"' | '\'' => {
                    self.string();
                    let kind = if self.expect_key {
                        Kind::Key
                    } else {
                        Kind::String
                    };
                    self.emit(kind, start);
                }
                '=' => {
                    self.i += 1;
                    self.expect_key = false;
                    self.emit(Kind::Punctuation, start);
                }
                '.' if self.expect_key => {
                    self.i += 1;
                    self.emit(Kind::Punctuation, start);
                }
                '[' | '{' => {
                    self.i += 1;
                    self.nesting.push(c);
                    self.expect_key = c == '{';
                    self.emit(Kind::Punctuation, start);
                }
                ']' | '}' => {
                    self.i += 1;
                    let open = if c == ']' { '[' } else { '{' };
                    if self.nesting.last() == Some(&open) {
                        self.nesting.pop();
                    }
                    self.expect_key = false;
                    self.emit(Kind::Punctuation, start);
                }
                ',' => {
                    self.i += 1;
                    self.expect_key = self.nesting.last() == Some(&'{');
                    self.emit(Kind::Punctuation, start);
                }
                c if is_word_char(c) => {
                    while self.peek(0).is_some_and(|c| {
                        is_word_char(c) || (!self.expect_key && matches!(c, '.' | ':'))
                    }) {
                        self.i += 1;
                    }
                    let kind = if self.expect_key {
                        Kind::Key
                    } else {
                        let word: String = self.chars[start..self.i].iter().collect();
                        value_kind(&word)
                    };
                    self.emit(kind, start);
                }
                _ => {
                    self.i += 1;
                    self.emit(Kind::Plain, start);
                }
            }
        }
    }

    fn skip_to_line_end(&mut self) {
        while self.peek(0).is_some_and(|c| c != '\n') {
            self.i += 1;
        }
    }

    /// `[table]` or `[[array.of.tables]]`, up to a trailing comment.
    fn table_header(&mut self) {
        let start = self.i;
        let mut quote = None;
        let mut end = self.i;
        while let Some(c) = self.peek(0) {
            match (quote, c) {
                (_, '\n') => break,
                (None, '#') => break,
                (None, '"' | '\'') => quote = Some(c),
                (Some(q), c) if c == q => quote = None,
                _ => {}
            }
            self.i += 1;
            if !c.is_whitespace() {
                end = self.i;
            }
        }
        let resume = self.i;
        self.i = end;
        self.emit(Kind::Table, start);
        self.i = resume;
    }

    /// A basic or literal string, single- or multi-line. An unterminated
    /// single-line string ends at the line end.
    fn string(&mut self) {
        let quote = self.chars[self.i];
        if self.peek(1) == Some(quote) && self.peek(2) == Some(quote) {
            self.i += 3;
            while let Some(c) = self.peek(0) {
                if c == '\\' && quote == '"' {
                    self.i += 2;
                } else if c == quote && self.peek(1) == Some(quote) && self.peek(2) == Some(quote) {
                    self.i += 3;
                    // Up to two quotes may sit just inside the delimiter.
                    for _ in 0..2 {
                        if self.peek(0) == Some(quote) {
                            self.i += 1;
                        }
                    }
                    break;
                } else {
                    self.i += 1;
                }
            }
            self.i = self.i.min(self.chars.len());
            return;
        }
        self.i += 1;
        while let Some(c) = self.peek(0) {
            match c {
                '\n' => return,
                '\\' if quote == '"' && self.peek(1).is_some_and(|c| c != '\n') => self.i += 2,
                c if c == quote => {
                    self.i += 1;
                    return;
                }
                _ => self.i += 1,
            }
        }
    }
}

/// Bare keys, and the building blocks of numbers, dates and booleans.
fn is_word_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '+')
}

fn value_kind(word: &str) -> Kind {
    let unsigned = word.trim_start_matches(['+', '-']);
    match word {
        "true" | "false" => Kind::Boolean,
        _ if unsigned.starts_with(|c: char| c.is_ascii_digit())
            || unsigned == "inf"
            || unsigned == "nan" =>
        {
            Kind::Number
        }
        _ => Kind::Plain,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(text: &str) -> Vec<(&'static str, String)> {
        tokenize(text)
            .into_iter()
            .map(|t| {
                let kind = match t.kind {
                    Kind::Plain => "plain",
                    Kind::Comment => "comment",
                    Kind::Table => "table",
                    Kind::Key => "key",
                    Kind::String => "string",
                    Kind::Number => "number",
                    Kind::Boolean => "boolean",
                    Kind::Punctuation => "punct",
                };
                (kind, t.text)
            })
            .collect()
    }

    fn pairs(expected: &[(&'static str, &str)]) -> Vec<(&'static str, String)> {
        expected.iter().map(|&(k, t)| (k, t.to_owned())).collect()
    }

    #[test]
    fn key_value_pairs() {
        assert_eq!(
            kinds("http_port = 2800 # the proxy\nenabled = true\n"),
            pairs(&[
                ("key", "http_port"),
                ("punct", "="),
                ("number", "2800"),
                ("comment", "# the proxy"),
                ("key", "enabled"),
                ("punct", "="),
                ("boolean", "true"),
            ])
        );
    }

    #[test]
    fn table_headers_keep_trailing_comments_apart() {
        assert_eq!(
            kinds("[services.api]\n[[services.api.routes]] # one route\n"),
            pairs(&[
                ("table", "[services.api]"),
                ("table", "[[services.api.routes]]"),
                ("comment", "# one route"),
            ])
        );
    }

    #[test]
    fn arrays_hold_values_across_lines() {
        assert_eq!(
            kinds("bind = [\n  \"127.0.0.1\", # v4\n  '::1',\n]\n[next]"),
            pairs(&[
                ("key", "bind"),
                ("punct", "="),
                ("punct", "["),
                ("string", "\"127.0.0.1\""),
                ("punct", ","),
                ("comment", "# v4"),
                ("string", "'::1'"),
                ("punct", ","),
                ("punct", "]"),
                ("table", "[next]"),
            ])
        );
    }

    #[test]
    fn inline_tables_alternate_keys_and_values() {
        assert_eq!(
            kinds("env = {PORT = \"8000\", \"a.b\".c = -1.5e3}"),
            pairs(&[
                ("key", "env"),
                ("punct", "="),
                ("punct", "{"),
                ("key", "PORT"),
                ("punct", "="),
                ("string", "\"8000\""),
                ("punct", ","),
                ("key", "\"a.b\""),
                ("punct", "."),
                ("key", "c"),
                ("punct", "="),
                ("number", "-1.5e3"),
                ("punct", "}"),
            ])
        );
    }

    #[test]
    fn dates_and_special_floats_are_numbers() {
        assert_eq!(
            kinds("a = 1979-05-27T07:32:00Z\nb = -inf\nc = oops"),
            pairs(&[
                ("key", "a"),
                ("punct", "="),
                ("number", "1979-05-27T07:32:00Z"),
                ("key", "b"),
                ("punct", "="),
                ("number", "-inf"),
                ("key", "c"),
                ("punct", "="),
                ("plain", "oops"),
            ])
        );
    }

    #[test]
    fn strings_handle_escapes_and_multiple_lines() {
        let tokens = tokenize("a = \"x\\\"y\" # c\nb = \"\"\"one\n  two\"\"\"\nc = 'open\nd = 1");
        let strings: Vec<_> = tokens
            .iter()
            .filter(|t| t.kind == Kind::String)
            .map(|t| (t.line, t.column, t.text.as_str()))
            .collect();
        assert_eq!(
            strings,
            [
                (0, 4, "\"x\\\"y\""),
                (1, 4, "\"\"\"one"),
                (2, 2, "two\"\"\""),
                (3, 4, "'open"),
            ]
        );
        assert_eq!(tokens.last().unwrap().kind, Kind::Number);
    }

    #[test]
    fn columns_count_characters() {
        let tokens = tokenize("k = \"é\" # ü\r\n");
        let placed: Vec<_> = tokens.iter().map(|t| (t.line, t.column)).collect();
        assert_eq!(placed, [(0, 0), (0, 2), (0, 4), (0, 8)]);
        assert_eq!(tokens[3].text, "# ü");
    }

    #[test]
    fn position_counts_characters_before_the_offset() {
        let text = "ab\nké\n";
        assert_eq!(position(text, 0), (0, 0));
        assert_eq!(position(text, 3), (1, 0));
        assert_eq!(position(text, 6), (1, 2));
        // Inside the two-byte é: the character it starts.
        assert_eq!(position(text, 5), (1, 1));
        assert_eq!(position(text, 99), (2, 0));
    }
}
