from flask import Flask, render_template, request, jsonify
import anthropic
import requests
from bs4 import BeautifulSoup
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

app = Flask(__name__)


def fetch_article(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
        tag.decompose()

    title = ''
    og_title = soup.find('meta', property='og:title')
    if og_title:
        title = og_title.get('content', '')
    if not title and soup.find('h1'):
        title = soup.find('h1').get_text().strip()
    if not title and soup.find('title'):
        title = soup.find('title').get_text().strip()

    meta_desc = ''
    og_desc = soup.find('meta', property='og:description')
    if og_desc:
        meta_desc = og_desc.get('content', '')
    if not meta_desc:
        m = soup.find('meta', attrs={'name': 'description'})
        if m:
            meta_desc = m.get('content', '')

    article_tag = soup.find('article')
    if article_tag:
        paragraphs = article_tag.find_all('p')
    else:
        content_div = (
            soup.find(class_=re.compile(r'article-body|post-content|entry-content|story-body|article-content', re.I)) or
            soup.find(id=re.compile(r'article-body|post-content|content|story', re.I))
        )
        paragraphs = content_div.find_all('p') if content_div else soup.find_all('p')

    content = ' '.join([
        p.get_text().strip() for p in paragraphs[:35]
        if len(p.get_text().strip()) > 40
    ])

    parsed = urlparse(url)
    source = parsed.netloc.replace('www.', '')

    return {
        'title': title,
        'content': content[:4000],
        'meta_description': meta_desc,
        'source': source,
    }


def process_with_claude(article, url, language, platform, tone, api_key):
    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are an expert social media content strategist and professional news rewriter.

ARTICLE DATA:
- URL: {url}
- Source: {article['source']}
- Title: {article['title']}
- Description: {article['meta_description']}
- Content: {article['content']}

REQUIREMENTS:
- Target Language: {language}
- Platform: {platform}
- Tone: {tone}
- Date: {datetime.now().strftime('%B %d, %Y')}

Return ONLY raw JSON — no markdown, no code fences:

{{
  "headline_options": [
    "Attention-grabbing headline in {language}",
    "Question-based headline in {language}",
    "Impact-focused headline in {language}",
    "Stat or number-based headline in {language}",
    "Human-interest angle headline in {language}"
  ],
  "summary": "Engaging 2-3 sentence summary in {language}",
  "key_points": [
    "Most important fact",
    "Second key point",
    "Third key point",
    "Context or implication"
  ],
  "caption": "Punchy {platform} caption under 140 characters in {language}",
  "hashtags": ["#tag1","#tag2","#tag3","#tag4","#tag5","#tag6","#tag7","#tag8","#tag9","#tag10"],
  "image_design_suggestion": {{
    "background_color": "#1a1a2e",
    "background_theme": "Specific visual description for ideal background image",
    "text_placement": "bottom",
    "font_color": "white",
    "overlay": "dark gradient from bottom, 70% opacity",
    "mood": "professional"
  }},
  "social_post_comment": "Complete ready-to-post text for {platform} in {language}. End with source: {url}",
  "engagement_tip": "One specific actionable tip to maximize engagement on {platform} for this story"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = message.content[0].text.strip()
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    return json.loads(text.strip())


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/process', methods=['POST'])
def process_article():
    try:
        data = request.json
        news_url = data.get('news_url', '').strip()
        language = data.get('language', 'English')
        platform = data.get('platform', 'Instagram')
        tone = data.get('tone', 'informative')
        api_key = data.get('api_key', '').strip() or os.getenv('ANTHROPIC_API_KEY', '')

        if not news_url:
            return jsonify({'error': 'Please provide a news URL'}), 400
        if not api_key:
            return jsonify({'error': 'Please provide your Anthropic API key'}), 400
        if not news_url.startswith(('http://', 'https://')):
            return jsonify({'error': 'URL must start with http:// or https://'}), 400

        article = fetch_article(news_url)
        if not article['content'] and not article['title']:
            return jsonify({'error': 'Could not extract content. This site may block scraping.'}), 400

        result = process_with_claude(article, news_url, language, platform, tone, api_key)
        result.update({
            'source': article['source'],
            'original_title': article['title'],
            'url': news_url,
            'platform': platform,
            'language': language,
            'processed_at': datetime.now().strftime('%B %d, %Y'),
        })
        return jsonify(result)

    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Could not connect to the URL. Check your internet connection.'}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timed out. The site took too long to respond.'}), 400
    except requests.exceptions.HTTPError as e:
        return jsonify({'error': f'HTTP {e.response.status_code} from the article URL'}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch article: {e}'}), 400
    except json.JSONDecodeError:
        return jsonify({'error': 'Failed to parse AI response. Please try again.'}), 500
    except anthropic.AuthenticationError:
        return jsonify({'error': 'Invalid Anthropic API key.'}), 401
    except anthropic.RateLimitError:
        return jsonify({'error': 'API rate limit hit. Try again in a moment.'}), 429
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') == 'development'
    print(f"\n  NewsForge Dashboard → http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=debug)
