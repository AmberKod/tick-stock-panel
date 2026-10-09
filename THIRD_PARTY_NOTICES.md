# Third-Party Notices

本仓包含以下第三方开源项目的代码摘取/修改。各项目许可证全文见其上游仓库。

## jiuzhang-algor（九章量化终端）

- **来源项目**: Ksyniko/jiuzhang-algor（本地: `D:\jiuzhang`）
- **许可证**: MIT License — Copyright (c) 2026 小鱼总的小圈子
- **摘取日期**: 2026-10-09
- **摘取模块**:

| 本仓路径 | 来源模块 | 说明 |
| :-- | :-- | :-- |
| `backend/app/data_providers/jiuzhang_us_fund_provider.py` | `us_fund.py` | SEC EDGAR XBRL 美股财报。原样移植, TSP 适配: 缓存目录改落 `settings.data_dir/us_fund_cache`; 全部请求经 `ProxyHandler({})` 直连 SEC (不走本机代理); 返回带 `source: "sec-edgar"` 过 source_gate 闸门。文件头保留上游 MIT 声明。 |

MIT 许可证全文（来自上游 `LICENSE`）:

```
MIT License

Copyright (c) 2026 小鱼总的小圈子

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
