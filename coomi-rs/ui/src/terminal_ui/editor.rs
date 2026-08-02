use unicode_width::UnicodeWidthChar;

#[derive(Clone, Debug, Default)]
pub struct Editor {
    content: Vec<char>,
    cursor: usize,
}

impl Editor {
    pub fn text(&self) -> String {
        self.content.iter().collect()
    }

    pub fn is_empty(&self) -> bool {
        self.content.is_empty()
    }

    pub fn clear(&mut self) {
        self.content.clear();
        self.cursor = 0;
    }

    pub fn take(&mut self) -> String {
        let text = self.text();
        self.clear();
        text
    }

    pub fn set(&mut self, text: impl AsRef<str>) {
        self.content = text.as_ref().chars().collect();
        self.cursor = self.content.len();
    }

    pub fn insert(&mut self, character: char) {
        self.content.insert(self.cursor, character);
        self.cursor += 1;
    }

    pub fn insert_str(&mut self, text: &str) {
        for character in text.chars() {
            self.insert(character);
        }
    }

    pub fn backspace(&mut self) {
        if self.cursor > 0 {
            self.cursor -= 1;
            self.content.remove(self.cursor);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor < self.content.len() {
            self.content.remove(self.cursor);
        }
    }

    pub fn move_left(&mut self) {
        self.cursor = self.cursor.saturating_sub(1);
    }

    pub fn move_right(&mut self) {
        self.cursor = (self.cursor + 1).min(self.content.len());
    }

    pub fn move_home(&mut self) {
        self.cursor = self.content[..self.cursor]
            .iter()
            .rposition(|character| *character == '\n')
            .map_or(0, |position| position + 1);
    }

    pub fn move_end(&mut self) {
        self.cursor = self.content[self.cursor..]
            .iter()
            .position(|character| *character == '\n')
            .map_or(self.content.len(), |position| self.cursor + position);
    }

    /// Move cursor up one logical line, trying to preserve column position.
    pub fn move_up(&mut self) {
        let (column, _row) = position_for(&self.content, self.cursor, u16::MAX);
        let line_start = self.content[..self.cursor]
            .iter()
            .rposition(|character| *character == '\n')
            .map_or(0, |position| position + 1);
        if line_start == 0 {
            self.cursor = 0;
            return;
        }
        let prev_line_start = self.content[..line_start - 1]
            .iter()
            .rposition(|character| *character == '\n')
            .map_or(0, |position| position + 1);
        self.cursor = seek_column(&self.content, prev_line_start, line_start - 1, column);
    }

    /// Move cursor down one logical line, trying to preserve column position.
    pub fn move_down(&mut self) {
        let (column, _row) = position_for(&self.content, self.cursor, u16::MAX);
        let line_end = self.content[self.cursor..]
            .iter()
            .position(|character| *character == '\n')
            .map_or(self.content.len(), |position| self.cursor + position);
        if line_end >= self.content.len() {
            self.cursor = self.content.len();
            return;
        }
        let next_line_end = self.content[line_end + 1..]
            .iter()
            .position(|character| *character == '\n')
            .map_or(self.content.len(), |position| line_end + 1 + position);
        self.cursor = seek_column(&self.content, line_end + 1, next_line_end, column);
    }

    /// Whether the content spans more than one logical line.
    #[allow(dead_code)]
    pub fn is_multiline(&self) -> bool {
        self.content.iter().any(|c| *c == '\n')
    }

    pub fn line_count(&self, width: u16) -> u16 {
        let (_, row) = position_for(&self.content, self.content.len(), width.max(1));
        row.saturating_add(1)
    }

    pub fn cursor_position(&self, width: u16) -> (u16, u16) {
        position_for(&self.content, self.cursor, width.max(1))
    }

    pub fn single_line_viewport(&self, width: u16, mask: Option<char>) -> (String, u16) {
        let width = usize::from(width.max(1));
        let available_before_cursor = width.saturating_sub(1);
        let mut start = self.cursor;
        let mut before_width = 0usize;
        while start > 0 {
            let character = mask.unwrap_or(self.content[start - 1]);
            let character_width = character.width().unwrap_or(0);
            if before_width.saturating_add(character_width) > available_before_cursor {
                break;
            }
            start -= 1;
            before_width = before_width.saturating_add(character_width);
        }
        let mut output = String::new();
        let mut used = 0usize;
        for character in self.content.iter().skip(start) {
            let display = mask.unwrap_or(*character);
            let character_width = display.width().unwrap_or(0);
            if used.saturating_add(character_width) > width {
                break;
            }
            output.push(display);
            used = used.saturating_add(character_width);
        }
        (output, u16::try_from(before_width).unwrap_or(u16::MAX))
    }
}

fn position_for(content: &[char], end: usize, width: u16) -> (u16, u16) {
    let mut column = 0_u16;
    let mut row = 0_u16;
    for character in content.iter().take(end) {
        if *character == '\n' {
            column = 0;
            row = row.saturating_add(1);
            continue;
        }
        let character_width = u16::try_from(character.width().unwrap_or(0)).unwrap_or(1);
        if column.saturating_add(character_width) > width {
            column = 0;
            row = row.saturating_add(1);
        }
        column = column.saturating_add(character_width);
        if column >= width {
            column = 0;
            row = row.saturating_add(1);
        }
    }
    (column, row)
}

/// Move to the position in `content[line_start..=line_end]` whose column is
/// as close to `target_column` as possible without exceeding it.
fn seek_column(content: &[char], line_start: usize, line_end: usize, target_column: u16) -> usize {
    let mut column = 0_u16;
    for index in line_start..=line_end {
        let character = if index < content.len() {
            content[index]
        } else {
            break;
        };
        if character == '\n' {
            break;
        }
        let character_width = u16::try_from(character.width().unwrap_or(0)).unwrap_or(1);
        if column.saturating_add(character_width) > target_column {
            return index;
        }
        column = column.saturating_add(character_width);
    }
    line_end.min(content.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edits_unicode_by_character() {
        let mut editor = Editor::default();
        editor.insert_str("你a");
        editor.move_left();
        editor.backspace();
        assert_eq!(editor.text(), "a");
    }

    #[test]
    fn tracks_wrapped_cursor_cells() {
        let mut editor = Editor::default();
        editor.insert_str("abcd你");
        assert_eq!(editor.cursor_position(5), (2, 1));
        assert_eq!(editor.line_count(5), 2);
    }

    #[test]
    fn single_line_viewport_keeps_cursor_visible() {
        let mut editor = Editor::default();
        editor.set("https://example.test/v1");
        let (visible, cursor) = editor.single_line_viewport(10, None);
        assert_eq!(visible, "e.test/v1");
        assert_eq!(cursor, 9);
        editor.move_left();
        let (masked, cursor) = editor.single_line_viewport(6, Some('*'));
        assert_eq!(masked, "******");
        assert_eq!(cursor, 5);
    }
}
