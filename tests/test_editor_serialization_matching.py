from chatgpt_web2api.turn_anchor import normalize_text, user_text_matches_sent


def test_editor_links_and_trailing_spaces_preserve_exact_source_prompt():
    prompt = 'Instruction. \n{"url":"https://example.org/path?q=moon", "content":"rotation\u00a0synchrone"}'
    stored = 'Instruction.&#x20;\n{"url":"[https://example.org/path?q=moon](https://example.org/path?q=moon)", "content":"rotation synchrone"}'
    assert user_text_matches_sent(stored, prompt)


def test_different_source_urls_or_link_labels_still_fail_closed():
    assert not user_text_matches_sent('[https://a.example](https://b.example)', 'https://a.example')
    assert not user_text_matches_sent('[NASA](https://example.org)', 'https://example.org')
    assert not user_text_matches_sent('rotation immobile', 'rotation synchrone')


def test_only_trailing_space_entities_are_normalized():
    assert normalize_text('literal &#x20; entity') == 'literal &#x20; entity'
