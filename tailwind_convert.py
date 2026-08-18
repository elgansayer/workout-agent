import os
import re

css_to_tailwind = {
    # Typography & Utils
    r'\bmuted\b': 'text-muted-foreground',
    r'\bhero\b': 'bg-gradient-to-br from-[#1e232c] to-[#171a21] border border-[#262c37] rounded-2xl p-4 md:p-6 mb-4 relative overflow-hidden transition-shadow duration-300 hover:shadow-[0_8px_40px_rgba(0,0,0,0.25)]',
    r'\bcycle\b': 'inline-block text-[0.78rem] uppercase tracking-widest text-green-400 relative',
    r'\bschemes\b': 'mt-3.5 flex flex-wrap gap-2',
    r'\bquote\b': 'mt-4 pt-3.5 border-t border-[#262c37] text-green-400 italic transition-colors hover:text-green-300',
    r'\brings\b': 'flex gap-4 items-center justify-center my-4',
    
    # Dashboard Grid
    r'\bdashboard-grid\b': 'grid grid-cols-[repeat(auto-fit,minmax(360px,1fr))] gap-5 items-start',
    r'\btoday\b': 'flex flex-col',
    
    # Exercises
    r'\bexercise-list\b': 'flex flex-col gap-2.5',
    r'\bexercise\b': 'bg-[#171a21] border border-[#262c37] rounded-xl p-3.5 transition-all duration-300 hover:border-green-400/20 hover:translate-x-0.5 hover:shadow-[0_2px_12px_rgba(0,0,0,0.15)]',
    r'\bex-head\b': 'flex justify-between items-baseline gap-4',
    r'\bex-name\b': 'font-semibold',
    r'\bex-planned\b': 'text-green-400 tabular-nums whitespace-nowrap',
    r'\bex-note\b': 'mt-1 text-muted-foreground text-[0.86rem]',
    r'\bex-meta\b': 'flex justify-between gap-4 mt-2 text-[0.85rem] flex-wrap',
    r'\blast\b': 'text-muted-foreground',
    r'\bnudge\b': 'text-green-400',
    
    # Lifestyle
    r'\blifestyle\b': 'bg-[#171a21] border border-[#262c37] rounded-xl p-4 md:p-5',
    r'\bpillars\b': 'm-0 pl-4 flex flex-col gap-1.5 marker:text-green-400',
    r'\brules\b': 'm-0 pl-4 flex flex-col gap-2 marker:text-green-400',
    
    # Reviews
    r'\breview-list\b': 'flex flex-col gap-2 my-2.5',
    r'\breview-row\b': 'grid grid-cols-[minmax(120px,1fr)_auto] [grid-template-areas:\'name_trend\'_\'detail_detail\'] gap-x-2.5 gap-y-1 bg-[#1e232c] border border-[#262c37] border-l-[3px] border-l-muted-foreground rounded-lg p-2.5',
    r'\breview-name\b': '[grid-area:name] font-semibold',
    r'\breview-trend\b': '[grid-area:trend] text-[0.72rem] font-semibold uppercase tracking-wide self-center',
    r'\breview-detail\b': '[grid-area:detail] text-[0.82rem] text-muted-foreground tabular-nums',
    r'\breview-recovery\b': 'mt-2.5 text-sm text-muted-foreground',
    r'\bstreak\b': 'text-lg font-bold text-green-400 mb-2',
    r'\bcal-scroll\b': 'overflow-x-auto',

    # Insights
    r'\binsight-header\b': 'bg-gradient-to-br from-[#1e232c]/80 to-[#171a21]/80 border border-green-400/15 border-t-2 border-t-green-400 shadow-[0_10px_30px_rgba(0,0,0,0.2)] relative overflow-hidden',
    r'\bai-gradient-text\b': 'bg-gradient-to-r from-sky-400 via-purple-500 to-green-400 bg-clip-text text-transparent font-bold',
    r'\binsight-content\b': 'text-[0.95rem] [&>p]:my-2.5 [&>p>strong]:text-slate-200 [&>p>strong]:mr-1.5',

    # Plan / Blocks
    r'\bblock-grid\b': 'grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3.5',
    r'\bblock-card\b': 'relative bg-[#1e232c] border border-[#262c37] rounded-xl p-4 transition-all duration-300 overflow-hidden hover:border-green-400/20 hover:shadow-[0_6px_24px_rgba(0,0,0,0.2)] hover:-translate-y-0.5',
    r'\bblock-schemes\b': 'list-none m-0 mt-3 p-0 flex flex-col gap-1.5 [&>li]:flex [&>li]:justify-between [&>li]:gap-2.5 [&>li]:text-[0.85rem] [&>li]:border-t [&>li]:border-[#262c37] [&>li]:pt-1.5 [&>li>span]:text-muted-foreground',
    
    # Days
    r'\bday-grid\b': 'grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3.5',
    r'\bday-card\b': 'bg-[#1e232c] border border-[#262c37] rounded-xl p-3.5 transition-all duration-300 hover:border-green-400/20 hover:-translate-y-[1px]',
    r'\bday-head\b': 'flex justify-between items-center',
    r'\bday-num\b': 'text-[0.75rem] uppercase tracking-wide text-muted-foreground',
    r'\bday-ex\b': 'list-none m-0 p-0 flex flex-col gap-1.5',
    r'\bday-ex li\b': 'flex justify-between gap-2.5 text-[0.84rem]',
    r'\bex-s\b': 'text-green-400 tabular-nums whitespace-nowrap',
    r'\brest\b': 'opacity-80',
    
    # Programmes
    r'\bprogramme-grid\b': 'grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-3.5',
    r'\bprogramme-card\b': 'bg-[#1e232c] border border-[#262c37] rounded-xl p-4 flex flex-col gap-2 transition-all duration-300 hover:border-green-400/20 hover:shadow-[0_6px_24px_rgba(0,0,0,0.2)] hover:-translate-y-0.5',
    r'\bactive-card\b': 'border-green-400 shadow-[0_0_20px_rgba(74,222,128,0.18)]',
    
    # History
    r'\blog-list\b': 'flex flex-col gap-3',
    r'\blog-row\b': 'bg-[#1e232c] border border-[#262c37] border-l-[3px] border-l-green-400 rounded-lg p-3 md:p-4 transition-all duration-300 hover:border-green-400/20 hover:shadow-[0_3px_16px_rgba(0,0,0,0.18)] hover:translate-x-0.5',
    r'\blog-head\b': 'flex items-center gap-2.5 flex-wrap mb-1.5',
    r'\blog-date\b': 'tabular-nums text-muted-foreground text-[0.85rem]',
    r'\blog-focus\b': 'font-semibold',
    r'\blog-body\b': 'm-0 whitespace-pre-wrap font-inherit text-[0.84rem] text-muted-foreground',
    
    # Checkins
    r'\bcheckin\b': 'bg-[#171a21] border border-[#262c37] rounded-xl p-3.5 mb-4 transition-all duration-300 hover:border-green-400/15 hover:shadow-[0_4px_20px_rgba(0,0,0,0.18)] hover:-translate-y-[1px]',
    r'\bcheckin-head\b': 'flex justify-between gap-4 mb-2 flex-wrap',
    r'\bcheckin-num\b': 'font-semibold text-green-400',
    r'\bcheckin-body\b': 'm-0 whitespace-pre-wrap font-inherit text-[0.9rem]',
    
    # Settings
    r'\bcard\b': 'bg-gradient-to-br from-[#1e232c] to-[#171a21] border border-[#262c37] rounded-2xl p-4 md:p-5 my-4',
    r'\bcard-head\b': 'flex items-center justify-between gap-4 mb-1.5',
    r'\bstatus\b': 'text-[0.74rem] font-bold uppercase tracking-wide px-2.5 py-1 rounded-full',
    r'\bon\b': 'text-green-400 bg-[#16261a]',
    r'\boff\b': 'text-muted-foreground bg-[#232936]',
    r'\bbtn-row\b': 'flex flex-wrap gap-2.5 items-center mt-3.5',
    r'\bbtn\b': 'inline-block bg-gradient-to-br from-green-400 to-[#36c06a] text-[#07210f] font-bold text-[0.9rem] no-underline border border-transparent rounded-lg px-4 py-2 cursor-pointer transition-all duration-300 shadow-[0_2px_8px_rgba(0,0,0,0.15)] hover:brightness-110 hover:-translate-y-[1px] hover:shadow-[0_6px_20px_rgba(74,222,128,0.2)]',
    r'\bbtn-ghost\b': 'bg-transparent text-white border-[#262c37] shadow-none hover:bg-[#1e232c] hover:shadow-[0_2px_8px_rgba(0,0,0,0.15)]',
    r'\bbanner\b': 'border border-[#262c37] rounded-lg px-3.5 py-2.5 my-1.5 font-[0.9rem]',
    r'\bok\b': 'border-green-400 text-green-400 bg-[#16261a]',
    r'\bwarn\b': 'border-amber-500 text-amber-500',
    
    # Stats
    r'\bstat-strip\b': 'grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] gap-3 mb-5',
    r'\bstat-card\b': 'bg-[#171a21] border border-[#262c37] rounded-xl p-3.5 flex flex-col items-center gap-1 text-center transition-all duration-500 relative overflow-hidden hover:-translate-y-1 hover:shadow-[0_8px_32px_rgba(0,0,0,0.3),0_0_1px_rgba(74,222,128,0.15)] hover:border-green-400/20',
    r'\bnum\b': 'text-[1.7rem] font-bold tabular-nums transition-colors duration-300 hover:text-green-400',
    r'\bnum-label\b': 'text-[0.78rem] text-muted-foreground',
    r'\bstat-cap\b': 'text-[0.72rem] uppercase tracking-wide text-muted-foreground mt-1',
    r'\bchart-grid\b': 'grid grid-cols-[repeat(auto-fit,minmax(300px,1fr))] gap-4',
    r'\bchart-card\b': 'bg-[#1e232c] border border-[#262c37] rounded-xl p-3.5 md:p-4 transition-all duration-300 hover:border-sky-400/20 hover:shadow-[0_4px_20px_rgba(0,0,0,0.2)] hover:-translate-y-[1px]',
    r'\bchart-card-head\b': 'flex justify-between items-center gap-2.5',
    r'\bchart-foot\b': 'text-[0.78rem] mt-2',
    r'\bdonut-wrap\b': 'flex items-center gap-5 flex-wrap',
    r'\blegend\b': 'flex flex-col gap-1.5 flex-1 min-w-[150px]',
    r'\blegend-row\b': 'flex items-center gap-2 text-[0.85rem]',
    r'\blegend-dot\b': 'w-3 h-3 rounded-sm',
    r'\blegend-label\b': 'flex-1',
    r'\blegend-val\b': 'text-muted-foreground tabular-nums',
    r'\bpr-table\b': 'w-full border-collapse text-[0.9rem]',
    r'\bnum-cell\b': 'text-green-400 tabular-nums whitespace-nowrap',
    
    # Chat
    r'\bchat-container\b': 'flex flex-col h-[calc(100vh-140px)] bg-[#171a21] border border-[#262c37] rounded-xl overflow-hidden',
    r'\bchat-messages\b': 'flex-1 overflow-y-auto p-4 flex flex-col gap-3',
    r'\bchat-msg\b': 'max-w-[85%] rounded-2xl p-3 text-[0.95rem] leading-relaxed shadow-sm',
    r'\bmsg-user\b': 'bg-[#1e232c] text-white self-end rounded-tr-sm border border-[#262c37]',
    r'\bmsg-ai\b': 'bg-gradient-to-br from-[#16261a] to-[#171a21] text-green-50 border border-green-400/20 self-start rounded-tl-sm shadow-[0_4px_12px_rgba(74,222,128,0.05)]',
    r'\bmsg-time\b': 'text-[0.7rem] opacity-60 mt-1.5 block text-right',
    r'\bchat-input-area\b': 'p-3 bg-[#1e232c] border-t border-[#262c37] flex gap-2 items-end',
    r'\bchat-input\b': 'flex-1 bg-[#0f1115] border border-[#262c37] rounded-xl p-3 text-white placeholder-muted-foreground outline-none focus:border-green-400 focus:shadow-[0_0_0_1px_rgba(74,222,128,0.2)] resize-none min-h-[44px] max-h-[150px]',
    r'\bchat-send\b': 'bg-green-400 text-[#07210f] border-none rounded-xl w-11 h-11 flex items-center justify-center cursor-pointer transition-all duration-300 hover:brightness-110 hover:scale-105 disabled:opacity-50 disabled:cursor-not-allowed',
}

def replace_classes_string(classes_str):
    words = classes_str.split()
    new_words = []
    for word in words:
        if word.startswith('trend-{{'):
            # This will be replaced entirely using ngClass later!
            # For now, drop it so it doesn't cause problems
            continue
            
        matched = False
        for pattern, replacement in css_to_tailwind.items():
            if re.fullmatch(pattern.replace(r'\b', ''), word):
                new_words.append(replacement)
                matched = True
                break
        if not matched:
            new_words.append(word)
    return " ".join(new_words)

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Add dynamic ngClass for trend logic in dashboard.html
    if 'dashboard.html' in filepath:
        # Instead of `class="..."`, we rewrite the review-row logic.
        content = content.replace(
            '''class="review-row trend-{{ lift.trend }}"''',
            '''class="review-row" [ngClass]="{'border-l-green-400 [&>.review-trend]:text-green-400': lift.trend === 'progressing', 'border-l-amber-500 [&>.review-trend]:text-amber-500': lift.trend === 'stalling', 'border-l-red-400 [&>.review-trend]:text-red-400': lift.trend === 'regressing', 'border-l-muted-foreground [&>.review-trend]:text-muted-foreground': lift.trend === 'new'}"'''
        )

    # Process all class attributes
    content = re.sub(r'(class\s*=\s*)(["\'])(.*?)\2', lambda m: m.group(1) + m.group(2) + replace_classes_string(m.group(3)) + m.group(2), content)

    with open(filepath, 'w') as f:
        f.write(content)

for root, _, files in os.walk('frontend/src/app/components'):
    for file in files:
        if file.endswith('.html'):
            process_file(os.path.join(root, file))

print("Conversion complete.")
