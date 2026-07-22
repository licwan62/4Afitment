export async function inspectControls(page) {
  return page.evaluate(() => {
    function labelFor(el) {
      const labels = [];
      if (el.id) {
        for (const label of document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`)) {
          labels.push(label.innerText.trim());
        }
      }
      const parentLabel = el.closest("label");
      if (parentLabel) labels.push(parentLabel.innerText.trim());

      const group = el.closest(".form-group,.field,.control,.select,.ant-form-item,.el-form-item,.v-select,.dropdown,[class*='form'],[class*='select']");
      if (group) labels.push(group.innerText.trim().replace(/\s+/g, " ").slice(0, 160));

      return [...new Set(labels.filter(Boolean))].join(" | ");
    }

    function cssPath(el) {
      if (el.id) return `#${CSS.escape(el.id)}`;
      const parts = [];
      let node = el;
      while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
        let part = node.nodeName.toLowerCase();
        if (node.classList.length) {
          part += `.${[...node.classList].slice(0, 3).map((x) => CSS.escape(x)).join(".")}`;
        }
        const parent = node.parentElement;
        if (parent) {
          const same = [...parent.children].filter((child) => child.nodeName === node.nodeName);
          if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
        }
        parts.unshift(part);
        node = parent;
      }
      return parts.join(" > ");
    }

    const selector = [
      "select",
      "input",
      "button",
      "[role='combobox']",
      "[aria-haspopup='listbox']",
      "[class*='select']",
      "[class*='dropdown']"
    ].join(",");

    return [...document.querySelectorAll(selector)]
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .map((el) => ({
        selector: cssPath(el),
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute("type") || "",
        role: el.getAttribute("role") || "",
        id: el.id || "",
        name: el.getAttribute("name") || "",
        placeholder: el.getAttribute("placeholder") || "",
        ariaLabel: el.getAttribute("aria-label") || "",
        text: (el.innerText || el.value || "").trim().replace(/\s+/g, " ").slice(0, 120),
        label: labelFor(el)
      }));
  });
}

export async function findControl(page, explicitSelectors, labelWords) {
  for (const selector of explicitSelectors ?? []) {
    if (await page.locator(selector).first().isVisible().catch(() => false)) return selector;
  }

  const controls = await inspectControls(page);
  const loweredWords = labelWords.map((word) => word.toLowerCase());

  const scored = controls
    .map((control) => {
      const haystack = [
        control.id,
        control.name,
        control.placeholder,
        control.ariaLabel,
        control.text,
        control.label
      ].join(" ").toLowerCase();

      let score = 0;
      for (const word of loweredWords) {
        if (haystack.includes(word)) score += word.length;
      }
      if (control.tag === "select") score += 5;
      if (control.role === "combobox") score += 4;
      if (/select|dropdown|combobox/i.test(control.selector)) score += 2;
      return { ...control, score };
    })
    .filter((control) => control.score > 0)
    .sort((a, b) => b.score - a.score);

  if (!scored.length) {
    throw new Error(`找不到下拉控件：${labelWords.join(" / ")}`);
  }

  return scored[0].selector;
}

export async function findYearRangeControls(page, selectors, labels) {
  const explicitFrom = selectors.yearFrom?.[0];
  const explicitTo = selectors.yearTo?.[0];

  if (explicitFrom && explicitTo) {
    return { from: explicitFrom, to: explicitTo };
  }

  const controls = await inspectControls(page);
  const yearWords = labels.year ?? ["year", "年份"];
  const loweredWords = yearWords.map((word) => word.toLowerCase());

  const yearControls = controls
    .map((control) => {
      const haystack = [
        control.id,
        control.name,
        control.placeholder,
        control.ariaLabel,
        control.text,
        control.label
      ].join(" ").toLowerCase();

      let score = 0;
      for (const word of loweredWords) {
        if (haystack.includes(word)) score += word.length;
      }
      if (control.tag === "select") score += 5;
      if (control.role === "combobox") score += 4;
      return { ...control, score };
    })
    .filter((control) => control.score > 0)
    .sort((a, b) => b.score - a.score);

  if (yearControls.length < 2) {
    const fallback = await findControl(page, selectors.yearFrom, labels.yearFrom ?? yearWords);
    return { from: fallback, to: fallback };
  }

  return {
    from: explicitFrom || yearControls[0].selector,
    to: explicitTo || yearControls[1].selector
  };
}

export async function findButton(page, explicitSelectors, labelWords) {
  for (const selector of explicitSelectors ?? []) {
    if (await page.locator(selector).first().isVisible().catch(() => false)) return page.locator(selector).first();
  }

  for (const label of labelWords) {
    const exact = page.getByRole("button", { name: label, exact: true }).first();
    if (await exact.isVisible().catch(() => false)) return exact;
  }

  for (const label of labelWords) {
    const fuzzy = page.getByRole("button", { name: new RegExp(escapeRegExp(label), "i") }).first();
    if (await fuzzy.isVisible().catch(() => false)) return fuzzy;
  }

  const textLocator = page.locator("button,a,[role='button']").filter({
    hasText: new RegExp(labelWords.map(escapeRegExp).join("|"), "i")
  }).first();
  if (await textLocator.isVisible().catch(() => false)) return textLocator;

  throw new Error(`找不到按钮：${labelWords.join(" / ")}`);
}

export async function getOptions(page, selector) {
  const locator = page.locator(selector).first();
  const tag = await locator.evaluate((el) => el.tagName.toLowerCase());

  if (tag === "select") {
    return locator.evaluate((el) => {
      return [...el.options]
        .map((option) => ({
          value: option.value,
          text: option.textContent.trim()
        }))
        .filter((option) => option.text && !/select|choose|please|请选择|全部/i.test(option.text));
    });
  }

  await locator.click();
  await page.waitForTimeout(300);

  const options = await page.evaluate(() => {
    const candidates = [
      ...document.querySelectorAll("[role='option'],li,.ant-select-item-option,.el-select-dropdown__item,.dropdown-item,.select-option,[class*='option']")
    ];

    return candidates
      .filter((el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .map((el) => ({
        text: el.innerText.trim().replace(/\s+/g, " "),
        selector: el.id ? `#${CSS.escape(el.id)}` : ""
      }))
      .filter((option) => option.text && !/select|choose|please|请选择|全部/i.test(option.text));
  });

  await page.keyboard.press("Escape").catch(() => {});
  return [...new Map(options.map((option) => [option.text, option])).values()];
}

export async function chooseOption(page, selector, option) {
  const locator = page.locator(selector).first();
  const tag = await locator.evaluate((el) => el.tagName.toLowerCase());

  if (tag === "select") {
    // Option values are site-generated IDs and can change between runs. Text is
    // stable, so resolve the current value from the live dropdown before selecting.
    if (option.text) await locator.selectOption({ label: option.text });
    else if (option.value) await locator.selectOption(option.value);
    else throw new Error(`控件 ${selector} 的选项缺少 text 和 value`);
    return;
  }

  await locator.click();
  await page.getByText(option.text, { exact: true }).last().click();
}

export async function chooseOptionIfNeeded(page, selector, option) {
  const current = await getSelectedOption(page, selector);
  if (option.text && normalizeOptionText(current.text) === normalizeOptionText(option.text)) return false;
  if (!option.text && current.value && option.value && current.value === option.value) return false;

  await chooseOption(page, selector, option);
  return true;
}

export async function waitForOptionsRefresh(page, selector, previousOptions = [], timeoutMs = 15000) {
  const previousSignature = optionsSignature(previousOptions);
  const deadline = Date.now() + timeoutMs;
  let latest = [];

  do {
    latest = await getOptions(page, selector);
    if (latest.length && optionsSignature(latest) !== previousSignature) return latest;
    await page.waitForTimeout(200);
  } while (Date.now() < deadline);

  if (latest.length) return latest;
  throw new Error(`控件 ${selector} 在 ${timeoutMs}ms 内没有加载出选项`);
}

export async function chooseOptionText(page, selector, text) {
  const options = await getOptions(page, selector);
  const option = options.find((item) => item.text === text)
    || options.find((item) => item.value === text)
    || options.find((item) => item.text.includes(text));

  if (!option) {
    throw new Error(`控件 ${selector} 中找不到选项：${text}`);
  }

  await chooseOption(page, selector, option);
}

export async function chooseOptionTextIfNeeded(page, selector, text) {
  const current = await getSelectedOption(page, selector);
  if (current.value === text || current.text === text || current.text.includes(text)) return false;

  await chooseOptionText(page, selector, text);
  return true;
}

export async function getSelectedOption(page, selector) {
  const locator = page.locator(selector).first();
  const tag = await locator.evaluate((el) => el.tagName.toLowerCase());

  if (tag === "select") {
    return locator.evaluate((el) => {
      const option = el.selectedOptions?.[0];
      return {
        value: el.value || "",
        text: option?.textContent?.trim() || ""
      };
    });
  }

  return locator.evaluate((el) => ({
    value: el.value || el.getAttribute("data-value") || "",
    text: (el.innerText || el.textContent || el.value || "").trim().replace(/\s+/g, " ")
  }));
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeOptionText(value) {
  return String(value ?? "").trim().replace(/\s+/g, " ").toLocaleLowerCase("en");
}

function optionsSignature(options) {
  return options.map((option) => `${option.value ?? ""}\t${normalizeOptionText(option.text)}`).join("\n");
}
