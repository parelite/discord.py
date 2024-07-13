class BasicEmoji:
    """Represnts a "basic" discord emoji.
    A basic emoji is either a unicode emoji or a custom emoji uploaded to the guild.
    
    Attributes
    -----------
    name: Optional[:class:`str`]
        The custom emoji name, if applicable, or the unicode codepoint
        of the non-custom emoji.
    """
    
    def __init__(self, name: str, is_custom_emoji: bool = False):
        self.name = name
        self.is_custom_emoji: bool = is_custom_emoji
        
    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} name={self.name!r}>'
    
    def __str__(self) -> str:
        return self.name
    
    def __eq__(self, other: object) -> bool:
        return self.name == other.name if isinstance(other, BasicEmoji) else False
    
    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)
    
    def __hash__(self) -> int:
        return hash(self.name)
    
    @property
    def is_unicode_emoji(self) -> bool:
        """:class:`bool`: Checks if this is a Unicode emoji."""
        return not self.is_custom_emoji
    
    